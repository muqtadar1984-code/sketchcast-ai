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
from shared.asset_keys import all_noise
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
        """The row was keyed `heart_anatomical` here until the contrast rule
        landed, and that pair is now REFUSED — see
        TestTwoKeysThatDenyEachOtherAreNotOneAnothersPicture for the pin and
        for what it costs. A qualifier the candidate simply does not carry is
        still fine: `anatomical` narrows the same heart, it does not name a
        different one."""
        _library(monkeypatch, tmp_path, {
            "asset_key": "human_heart_anatomical",
            "canonical_key": "anatomical_heart_human",
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
        assert hit["asset_key"] == "human_heart_anatomical"
        assert hit["match_score"] >= vl.threshold_now()
        # and it would survive the 1.25 stopgap threshold too
        assert hit["match_score"] >= 1.25

    def test_a_reworded_request_still_finds_its_asset(
            self, tmp_path, monkeypatch):
        """The request key here was `erupting_volcano_diagram` until the
        contrast rule landed. An eruption and a cut-away are two pictures, and
        the library holds both `volcano_cross_section` and
        `composite_volcano_cross_section` — so that pair is refused now, and
        pinned as a refusal below. Rewording that does not introduce a rival
        claim still finds the asset."""
        _library(monkeypatch, tmp_path, {
            "asset_key": "volcano_cross_section",
            "canonical_key": "cross_section_volcano",
            "description": ("A cross-section of a volcano showing the magma "
                            "chamber, the central vent and the cone"),
            "subject": "geography", "grade": "k12", "curriculum": "generic",
            "topic": "volcano cross section", "concepts": [],
            "status": "approved", "asset_type": "visual",
            "local_cache_path": "/tmp/v.png"})
        assert vl.find("volcano_diagram",
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


class TestABareNumberNamesNothing:
    """`figure_3` and `diagram_3` both reduced to the single core token "3",
    so they shared a canonical key — one cache directory, and one row in the
    SHARED cross-book library. That is one book's figure 3 being served for
    another book's, which is the wrong-picture class this whole file exists to
    close, arriving through the cache rather than through the score."""

    def test_two_books_figure_three_are_two_pictures(self):
        assert vl.canonical_key("figure_3") != vl.canonical_key("diagram_3")
        assert vl.canonical_key("figure_3") != vl.canonical_key("3")
        assert vl.canonical_key("figure_1") != vl.canonical_key("figure_2")

    def test_the_number_is_kept_not_dropped(self):
        """Dropping it would be the same collapse from the other side:
        `figure_3` and `figure_4` would become one directory."""
        assert "3" in vl.canonical_key("figure_3").split("_")
        assert "figure" in vl.canonical_key("figure_3").split("_")

    def test_a_numbered_subject_is_unchanged(self):
        """`stage_3` names a subject, so the fold is exactly as it was: the
        numeral stays and separates it from `stage_2`."""
        assert vl.canonical_key("stage_3") != vl.canonical_key("stage_2")
        assert set(vl.canonical_key("stage_3").split("_")) == {"stage", "3"}
        assert set(vl.canonical_key("avatar_student_11_12_f").split("_")) == \
            {"avatar", "student", "11", "12", "f"}

    def test_a_number_says_nothing_about_which_picture(self):
        from shared.asset_keys import distinguishes
        assert all_noise("figure_3"), "a medium word and an index: nothing"
        assert not distinguishes("3")
        assert not distinguishes("diagram")
        assert distinguishes("cell"), "folded for the cache, but it DOES narrow"
        assert distinguishes("ciliated")

    def test_a_shared_numeral_is_not_a_match_on_the_strict_path_either(self):
        """`guard_tokens` keeps numerals — they are what separates `stage_2`
        from `stage_3` in the cache — so two keys that DO name subjects could
        still be paired by nothing but the number they carry."""
        row = {"asset_key": "phase_3", "canonical_key": "3_phase"}
        assert vl.guard_tokens("stage_3") & vl.guard_tokens("phase_3") == {"3"}
        assert vl.key_guard_ok("stage_3", row) is False
        assert vl.key_guard_ok("stage_3", {"asset_key": "stage_3_diagram",
                                           "canonical_key": "3_stage"}) is True

    def test_the_renderer_and_the_library_still_agree(self):
        for key in ("figure_3", "diagram_3", "stage_3", "figure_3_2"):
            assert vl.canonical_key(key) == renderer_key(key) == \
                shared_key(key), key


class TestTheAbstentionMustRestOnSomething:
    """The abstention path exists so `cell_diagram` can still be answered by
    `animal_cell_diagram`. It compared RAW tokens, and every key in the library
    carries a medium word — so an all-noise request was eligible for the whole
    catalogue on the strength of "diagram", with only the threshold left in the
    way. At least one shared token must name something."""

    _VOLCANO = {
        "asset_key": "volcano_diagram", "canonical_key": "volcano",
        "description": ("An educational diagram of a volcano showing the "
                        "magma chamber, the vent and the crater."),
        "subject": "geography", "grade": "k12", "curriculum": "generic",
        "topic": "volcano", "concepts": ["volcano"], "status": "approved",
        "asset_type": "visual", "local_cache_path": "/tmp/v.png",
    }
    _ANIMAL_CELL = {
        "asset_key": "animal_cell_diagram", "canonical_key": "animal",
        "description": ("An educational diagram of an animal cell showing the "
                        "nucleus, cytoplasm and cell membrane."),
        "subject": "biology", "grade": "k12", "curriculum": "generic",
        "topic": "animal cell", "concepts": ["cell"], "status": "approved",
        "asset_type": "visual", "local_cache_path": "/tmp/ac.png",
    }

    def test_a_shared_medium_word_is_not_a_match(self):
        """The noise-only request that must NOT match."""
        assert all_noise("cell_diagram")
        assert vl.key_guard_ok("cell_diagram", self._VOLCANO) is False

    def test_nor_is_a_shared_article(self):
        for key in ("the_diagram", "a_picture_of_the", "an_illustration"):
            assert all_noise(key), key
            assert vl.key_guard_ok(key, self._VOLCANO) is False, key
            assert vl.key_guard_ok(key, self._ANIMAL_CELL) is False, key

    def test_nor_is_a_shared_index_number(self):
        row = {"asset_key": "diagram_3", "canonical_key": "3_diagram"}
        assert vl.key_guard_ok("figure_3", row) is False

    def test_the_volcano_is_not_served_for_a_cell_diagram(
            self, tmp_path, monkeypatch):
        _library(monkeypatch, tmp_path, self._VOLCANO)
        assert vl.find("cell_diagram",
                       "An educational diagram of a volcano showing the magma "
                       "chamber, the vent and the crater",
                       min_score=0.0) is None

    def test_the_match_the_abstention_exists_for_still_passes(self):
        """"cell" is folded for the cache, but it narrows the subject: it is
        allowed to carry the abstention where "diagram" alone is not."""
        assert vl.key_guard_ok("cell_diagram", self._ANIMAL_CELL) is True

    def test_and_the_library_still_serves_it(self, tmp_path, monkeypatch):
        _library(monkeypatch, tmp_path, self._ANIMAL_CELL)
        assert vl.find("cell_diagram",
                       "An educational diagram of an animal cell showing the "
                       "nucleus, cytoplasm and cell membrane") is not None

    def test_a_key_with_nothing_distinguishing_still_matches_itself(self):
        """`figure_3` shares only a medium word and an index with anything —
        including its own row. Same canonical key is the same picture."""
        for key in ("figure_3", "cell_diagram", "the_diagram"):
            assert vl.key_guard_ok(key, {
                "asset_key": key, "canonical_key": vl.canonical_key(key)}), key

    def test_the_refusal_is_logged(self, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="shared.visual_library"):
            assert vl.key_guard_ok("cell_diagram", self._VOLCANO) is False
        assert any("name a medium or an index" in r.getMessage()
                   for r in caplog.records), caplog.text


def _chem(key: str, canonical: str, description: str) -> dict:
    return {"asset_key": key, "canonical_key": canonical,
            "description": description, "subject": "chemistry",
            "grade": "14-16", "curriculum": "generic", "topic": key,
            "concepts": ["energy"], "status": "approved",
            "asset_type": "visual", "local_cache_path": f"/tmp/{key}.png"}


# The three real rows, carrying their live descriptions. Near-identical prose
# describing opposite pictures, which is the whole reason the score cannot
# separate them and the KEY has to.
_ENDOTHERMIC = _chem(
    "endothermic_energy_profile", "endothermic_energy_profile",
    "A blank pair of axes with a flat line on the left at a low level, a flat "
    "line on the right at a higher level, a curved hump rising from the left "
    "line and settling on the right line, a vertical arrow from the left line "
    "to the top of the hump and a second vertical arrow from the left line up "
    "to the right line. Write no numbers or words. Name the layer groups "
    "exactly: vertical axis, horizontal axis, reactants level.")
_EXOTHERMIC = _chem(
    "exothermic_energy_profile", "energy_exothermic_profile",
    "A blank pair of axes with a flat line on the left at a high level, a flat "
    "line on the right at a lower level, a curved hump rising from the left "
    "line and falling to the right line, a vertical arrow from the left line "
    "to the top of the hump and a second vertical arrow from the left line "
    "down to the right line. Write no numbers or words. Name the layer groups "
    "exactly: vertical axis, horizontal axis, reactants level.")
_CATALYST = _chem(
    "catalyst_energy_profile", "catalyst_energy_profile",
    "A blank pair of axes with a flat line on the left for the starting level, "
    "a lower flat line on the right for the finishing level, and two curved "
    "humps joining them, a tall solid hump and a lower dashed hump, with a "
    "vertical arrow measuring the height of each hump from the starting "
    "level. Write no numbers or words. Name the layer groups exactly: "
    "vertical axis, horizontal axis, reactants level.")


class TestTwoKeysThatDenyEachOtherAreNotOneAnothersPicture:
    """On 2026-09-05 the library served `catalyst_energy_profile` to BOTH
    `endothermic_energy_profile` and `exothermic_energy_profile`. All three
    share "energy" and "profile", so the guard's inclusive test passed and
    only the threshold was left — and in the production context those pairs
    score 0.79 and 0.82, twice over the 0.58 default. Three chemically
    distinct diagrams, one picture served for all of them, on a classroom
    board, confidently.

    This one could not be closed by subtracting a class of token the way "sk",
    the connectives and the layer tail were: "energy" and "profile" are what
    these keys are ABOUT. What separates them is the token each key carries
    that the other does not.

    Measured over all 100,172 ordered pairs of the 317 approved visual rows,
    each row's own description as the prompt and no explicit caller context
    (which is what production runs — `set_context` is called from nowhere):
    the guard admitted 1,936 pairs and the contrast rule refuses 1,506 of
    them; 272 of the 448 admissions clearing the 0.58 default are contrast
    pairs. Simulating find(), that costs 59 of 125 served requests their
    answer — every one a different picture — and changes 9 answers, of which
    8 are neutral or better and one (`plant_cell_outline__merged`) is worse.
    The full accounting, and why 0.58 rather than 0.85 is the column to read,
    is the comment above `_unmatched` in shared/visual_library.py.
    """

    def test_all_three_energy_profiles_refuse_each_other(self):
        rows = (_ENDOTHERMIC, _EXOTHERMIC, _CATALYST)
        for q in rows:
            for c in rows:
                if q is c:
                    continue
                assert vl.key_guard_ok(q["asset_key"], c) is False, \
                    f'{q["asset_key"]} <- {c["asset_key"]}'

    def test_the_catalyst_curve_is_not_served_to_either_reaction(
            self, tmp_path, monkeypatch):
        """The reported incident, both halves of it."""
        _library(monkeypatch, tmp_path, _CATALYST)
        for row in (_ENDOTHERMIC, _EXOTHERMIC):
            assert vl.find(row["asset_key"], row["description"]) is None
            # …and not because the threshold happened to save us
            assert vl.find(row["asset_key"], row["description"],
                           min_score=0.0) is None

    def test_the_worst_pair_is_the_one_nobody_reported(
            self, tmp_path, monkeypatch):
        """Measured at 1.23 — above the catalyst pair that WAS reported. The
        library would hand an exothermic profile, products BELOW the
        reactants, to a request for an endothermic one, products above."""
        _library(monkeypatch, tmp_path, _EXOTHERMIC)
        row, score, _ = vl.best_match("endothermic_energy_profile",
                                      _ENDOTHERMIC["description"])
        assert row is not None and score >= 0.85, \
            "the score alone was never going to refuse this"
        assert vl.find("endothermic_energy_profile",
                       _ENDOTHERMIC["description"], min_score=0.0) is None

    def test_the_near_miss_is_still_recorded_as_evidence(
            self, tmp_path, monkeypatch):
        """find() refuses it; best_match must still see it, or "is 0.85 the
        right threshold" stops being answerable."""
        _library(monkeypatch, tmp_path, _CATALYST)
        row, score, source = vl.best_match("endothermic_energy_profile",
                                           _ENDOTHERMIC["description"])
        assert row["asset_key"] == "catalyst_energy_profile"
        assert score > 0 and source == "local"

    def test_plant_and_animal_cells_are_different_pictures(self):
        """Passes on the baseline too, and it is worth saying which rule does
        the work: "cell" is KEY_NOISE, so the two keys reduce to {plant} and
        {animal} and share NO token — the contrast branch is never reached.
        What separates them is the older shared-token rule. Kept because the
        pair must stay separated however the guard is rearranged; asserted
        with its precondition so it cannot be misread as evidence for the
        contrast rule, and so that promoting "cell" out of KEY_NOISE fails
        here rather than silently changing what this test means."""
        assert vl.guard_tokens("plant_cell") & vl.guard_tokens("animal_cell") \
            == set(), "reaching the contrast branch would change this test"
        for a, b in (("plant_cell", "animal_cell"),
                     ("animal_cell", "plant_cell")):
            assert vl.key_guard_ok(a, {"asset_key": b,
                                       "canonical_key": vl.canonical_key(b)}) \
                is False, f"{a} <- {b}"

    @pytest.mark.parametrize("query,candidate", [
        ("meiosis_stages", "mitosis_stages"),
        ("covalent_bonding_dot_and_cross_methane",
         "covalent_bonding_dot_and_cross_water"),
        ("capillary_exchange", "alveolus_gas_exchange"),
        ("nitrogen_cycle", "carbon_cycle"),
        ("concave_mirror_ray_diagram", "converging_lens_ray_diagram"),
        ("constructive_and_destructive_waves", "constructive_plate_boundary"),
        ("red_blood_cell__merged", "plant_cell_wall__merged"),
        ("digestive_system", "respiratory_system"),
    ])
    def test_the_rest_of_the_wrong_pictures_this_closes(self, query, candidate):
        """Every one of these is a real live pair the library admits today
        and scores over the 0.58 default. The last two are worth naming:
        `constructive_and_destructive_waves` is two waves on a beach and
        `constructive_plate_boundary` is a sea-floor ridge; and
        `red_blood_cell__merged` <- `plant_cell_wall__merged` is the
        `ciliated_cell` bug verbatim — the ONLY token those two keys share is
        the pipeline suffix "merged", and it scores 0.99."""
        assert vl.key_guard_ok(query, {
            "asset_key": candidate,
            "canonical_key": vl.canonical_key(candidate)}) is False

    def test_an_eruption_is_not_a_cut_away(self):
        """Moved out of TestGenuineReuseStillWorks, where it asserted the
        opposite. Two different specialisations of "volcano" are two different
        pictures, and the library holds both cross-sections already."""
        assert vl.key_guard_ok("erupting_volcano_diagram", {
            "asset_key": "volcano_cross_section",
            "canonical_key": "cross_section_volcano"}) is False

    def test_what_this_costs_when_two_qualifiers_mean_the_same_thing(self):
        """The honest price of a key-level rule, pinned so it is not
        discovered by surprise. `human` and `anatomical` narrow the SAME
        heart, but nothing in the two KEYS says so, while `endothermic` and
        `catalyst` name different curves — so this reuse is refused and the
        heart is paid for again.

        Measured, the class barely occurs at the point of service: of the
        272 contrast refusals that clear the 0.58 default, the only pair
        holding the same picture is `leaf_microscope_view` /
        `microscope_split_view` (identical descriptions), and neither board
        loses its diagram, because `microscope_view` answers both by subset at
        1.21. A re-SPELLING is a different matter and is not refused at all —
        see TestASpellingIsNotARivalClaim."""
        assert vl.key_guard_ok("human_heart_diagram", {
            "asset_key": "heart_anatomical",
            "canonical_key": "anatomical_heart"}) is False


class TestSpecialisationIsNotContrast:
    """The rule is symmetric, so BOTH pure-subset directions stay open: a key
    whose tokens the other simply contains is narrowing the same subject, not
    denying it. Measured at the production threshold the two directions hold
    31 and 16 pairs and are each about half genuine reuse and half
    reduced-variant error, so closing either would cost as much correct reuse
    as it saves."""

    _COMPOSITE = {"asset_key": "composite_volcano_cross_section",
                  "canonical_key": "composite_cross_section_volcano"}
    _GENERIC = {"asset_key": "volcano_cross_section",
                "canonical_key": "cross_section_volcano"}

    def test_a_more_specific_candidate_may_answer_a_general_request(self):
        assert vl.key_guard_ok("volcano_cross_section", self._COMPOSITE) is True

    def test_and_the_general_one_may_answer_the_specific_request(self):
        """Asked explicitly in the brief, and left open: `volcano_cross_section`
        is the same cut-away with one fewer claim on it. Closing this direction
        would also refuse `human_ciliated_cell_diagram <- ciliated_cell_asset`
        (1.23) and `microscope_split_view <- microscope_view` (1.21), which are
        the same picture — 15 answers lost and 2 changed at the 0.58
        default, against no wrong picture prevented."""
        assert vl.key_guard_ok("composite_volcano_cross_section",
                               self._GENERIC) is True

    def test_an_exact_key_is_untouched(self):
        for key in ("endothermic_energy_profile", "plant_cell", "sk_boat"):
            assert vl.key_guard_ok(key, {
                "asset_key": key, "canonical_key": vl.canonical_key(key)}), key


class TestASpellingIsNotARivalClaim:
    """A key written differently is not a key that denies you.

    The keys are free text a MODEL invents, over a product shipping Cambridge,
    CBSE and US curricula in ten locales, so the same picture arrives spelled
    more than one way — and the LIBRARY is not consistent either: it holds
    `addition_polymerisation_of_ethene`, `muscle_fibre` and
    `fertilisation_oviduct` beside `copper_sulfate`, `fetus_uterus` and
    `organization_hierarchy`.

    A plain set difference reads every one of those as a contrast. Measured
    against the 317 live rows by asking find() for each key with one word
    respelled: 36 library keys become unreachable through a regular English
    plural the Latin table does not cover, and 11 more through the other
    orthography — every one at a score clearing 0.85, every one a paid
    regeneration (~US$0.04, ~53 s) for a picture the library already holds, on
    the quota that is the binding constraint. So the residual is computed
    through `asset_keys.same_word`.
    """

    @pytest.mark.parametrize("query,stored", [
        # the regular -y -> -ies rule partnames' Latin table cannot see
        ("capillaries_exchange", "capillary_exchange"),
        ("constructive_plate_boundaries", "constructive_plate_boundary"),
        ("organization_hierarchies", "organization_hierarchy"),
        ("athenian_democracies_structure", "athenian_democracy_structure"),
        # -f/-fe -> -ves and the irregulars
        ("leaves_cross_section", "leaf_cross_section_diagram"),
        ("starch_test_leaves", "starch_test_leaf"),
        ("teeth_structure", "tooth_structure"),
        ("mosquito_lives_cycle_malaria", "mosquito_life_cycle_malaria"),
        # British/American — both directions, because the library holds both
        ("extraction_of_aluminum", "extraction_of_aluminium"),
        ("displacement_reaction_iron_nail_copper_sulphate",
         "displacement_reaction_iron_nail_copper_sulfate"),
        ("foetus_uterus", "fetus_uterus"),
        ("muscle_fiber", "muscle_fibre"),
        ("biological_organisation_hierarchy", "organization_hierarchy"),
        ("addition_polymerization_of_ethene",
         "addition_polymerisation_of_ethene"),
        ("specialized_animal_cells_table", "specialised_animal_cells_table"),
        ("fertilization_oviduct", "fertilisation_oviduct"),
        ("evaporation_and_crystallization", "evaporation_and_crystallisation"),
    ])
    def test_the_same_picture_spelled_differently_is_still_a_candidate(
            self, query, stored):
        assert vl.key_guard_ok(query, {
            "asset_key": stored,
            "canonical_key": vl.canonical_key(stored)}) is True, \
            f"{query} <- {stored}"

    def test_and_the_library_actually_serves_it(self, tmp_path, monkeypatch):
        """Through find(), at the production threshold, on the real row: the
        guard is only half the path and a refusal here is a paid image."""
        _library(monkeypatch, tmp_path, {
            "asset_key": "leaf_cross_section_diagram",
            "canonical_key": "cross_leaf_section",
            "description": ("A cross-section of a leaf showing the upper "
                            "epidermis, the palisade layer, the spongy layer "
                            "and the lower epidermis"),
            "subject": "biology", "grade": "k12", "curriculum": "generic",
            "topic": "leaf cross section", "concepts": [],
            "status": "approved", "asset_type": "visual",
            "local_cache_path": "/tmp/leaf.png"})
        hit = vl.find("leaves_cross_section",
                      "A cross-section of a leaf showing the upper epidermis, "
                      "the palisade layer, the spongy layer and the lower "
                      "epidermis", min_score=0.85)
        assert hit is not None and hit["asset_key"] == "leaf_cross_section_diagram"

    def test_a_plural_does_not_deny_its_own_singular(self):
        assert vl.key_guard_ok("organ_system_diagram", {
            "asset_key": "organs_to_system",
            "canonical_key": "organs_system"}) is True
        assert vl.key_guard_ok("organs_to_system", {
            "asset_key": "organ_system_diagram",
            "canonical_key": "organ_system"}) is True

    def test_a_latin_plural_does_not_either(self):
        assert vl.key_guard_ok("mitochondrion_structure", {
            "asset_key": "mitochondria_structure",
            "canonical_key": "mitochondria_structure"}) is True

    def test_the_helper_answers_directly(self):
        assert vl._unmatched({"organ", "system"}, {"organs", "system"}) == set()
        assert vl._unmatched({"leaf", "section"}, {"leaves", "section"}) == set()
        assert vl._unmatched({"aluminum"}, {"aluminium"}) == set()
        assert vl._unmatched({"endothermic", "energy"},
                             {"catalyst", "energy"}) == {"endothermic"}

    def test_a_compound_the_model_split_is_the_same_key(self):
        """The live library holds `backtoback_housing_cross_section`; a book
        that writes `back_to_back_housing_cross_section` shares NO token with
        it ("back" against "backtoback"), so the contrast rule read a rival
        claim and refused a row scoring 1.40. The canonical key cannot catch
        it — it sorts tokens, and these two have different ones."""
        assert vl.key_guard_ok("back_to_back_housing_cross_section", {
            "asset_key": "backtoback_housing_cross_section",
            "canonical_key": "backtoback_cross_housing_section"}) is True
        assert vl.key_guard_ok("backtoback_housing_cross_section", {
            "asset_key": "back_to_back_housing_cross_section",
            "canonical_key": "back_cross_housing_section_to"}) is True

    def test_but_only_the_same_letters_in_the_same_order(self):
        """Not an anagram, not a prefix: it is one name punctuated twice."""
        assert vl.key_guard_ok("housing_back_to_back_cross_section", {
            "asset_key": "backtoback_housing_cross_section",
            "canonical_key": "backtoback_cross_housing_section"}) is False


class TestTheFoldIsSpellingsAndInflectionsAndNothingElse:
    """The rule that keeps the loosening honest, pinned THROUGH THE GUARD.

    `same_word` is not a similarity measure and must never become one. The
    tempting simplification is containment — "one token contains the other, so
    they are the same word" — which is `partnames.resolve_part`'s third tier
    and reads like a drop-in. Over the 508 distinct guard tokens in the live
    library that tier folds 90 extra pairs, including female/male, cat/catalyst,
    ant/plant, organ/organism, ear/heart, carbon/hydrocarbon and stem/system.
    Every assertion below goes through `key_guard_ok`, so the mutation fails
    here and not only in a helper's unit test.
    """

    @pytest.mark.parametrize("query,candidate", [
        ("male_reproductive_system", "female_reproductive_system"),
        ("female_reproductive_system", "male_reproductive_system"),
        ("cat_skeleton", "catalyst_energy_profile"),
        ("ant_anatomy", "plant_cell_wall"),
        ("organ_transplant", "organism_classification"),
        ("carbon_cycle", "hydrocarbon_cracking"),
        ("stem_cross_section", "system_overview"),
    ])
    def test_containment_is_not_a_fold(self, query, candidate):
        assert vl.key_guard_ok(query, {
            "asset_key": candidate,
            "canonical_key": vl.canonical_key(candidate)}) is False, \
            f"{query} <- {candidate}"

    def test_it_never_folds_a_real_contrast(self):
        """The pairs partnames' own docstring warns a spelling tier gets
        wrong. Over the live vocabulary `same_word` folds exactly the 18 pairs
        `same_part` folds — all singular/plural of one word — and no more."""
        from shared.asset_keys import same_word
        for a, b in (("meiosis", "mitosis"), ("nucleolus", "nucleus"),
                     ("neutron", "neuron"), ("endothermic", "exothermic"),
                     ("alkane", "alkene"), ("male", "female"),
                     ("fetus", "uterus"), ("concave", "convex")):
            assert not same_word(a, b), f"{a}/{b}"
        assert vl.key_guard_ok("meiosis_stages", {
            "asset_key": "mitosis_stages",
            "canonical_key": "mitosis_stages"}) is False

    def test_it_folds_everything_the_renderer_s_matcher_folds(self):
        """Anti-drift. `same_word` answers a wider question than
        `partnames.same_part` and lives in a different module, so the two
        could disagree about a plural — which is how this codebase got two
        canonical_key functions. They may not: whatever the renderer calls one
        name, the guard must too."""
        from shared.asset_keys import same_word
        from spike.scene_engine.partnames import same_part
        for a, b in (("organ", "organs"), ("bacterium", "bacteria"),
                     ("nucleus", "nuclei"), ("villus", "villi"),
                     ("ovum", "ova"), ("mitochondria", "mitochondrion"),
                     ("analysis", "analyses"), ("tissue", "tissues"),
                     ("neurone", "neuron"), ("membrane", "membranes")):
            assert same_part(a, b), f"partnames changed: {a}/{b}"
            assert same_word(a, b), f"same_word is behind same_part: {a}/{b}"


class TestTheGuardDoesNotDragInTheRenderer:
    """`shared` may not import `spike.scene_engine`.

    Importing any module of that package runs its `__init__`, whose last
    statement imports `visual_library_integration` — whose module body calls
    `_patch()`: it reindexes storage/scene_assets into the local library index
    (a read and rewrite of index.json per cached asset) and monkeypatches
    `raster_assets.get_raster_asset` and `svg_assets.get_svg_asset`. A
    function-local import does not avoid that, it defers it — onto the one
    function in this module that is a pure predicate. So the word fold lives
    in `shared.asset_keys`, which has no side effects at all.
    """

    def test_calling_the_guard_does_not_import_the_scene_engine(self):
        import subprocess
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        script = (
            "import sys\n"
            "import shared.visual_library as vl\n"
            "before = 'spike.scene_engine' in sys.modules\n"
            "vl.key_guard_ok('endothermic_energy_profile', "
            "{'asset_key': 'catalyst_energy_profile', "
            "'canonical_key': 'catalyst_energy_profile'})\n"
            "vl.key_guard_ok('leaves_cross_section', "
            "{'asset_key': 'leaf_cross_section', "
            "'canonical_key': 'cross_leaf_section'})\n"
            "after = 'spike.scene_engine' in sys.modules\n"
            "print(before, after)\n")
        out = subprocess.run([sys.executable, "-c", script], cwd=str(root),
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert out.stdout.split() == ["False", "False"], out.stdout


class TestTheAbstentionPathIsUntouched:
    """The contrast test is applied ONLY where the guard admitted on a shared
    distinguishing token. Applied on the abstention path it would refuse
    `cell_diagram <- animal_cell_diagram` — the named regression — because an
    all-noise key keeps its noise words while the row has had them stripped,
    so both residuals are non-empty by construction.

    Empirically the abstention path was taken by 0 of the 100,172 live pairs:
    no key in the library is all-noise. It is exercised only by request keys a
    book invents, which is exactly when it must not be tightened."""

    _ANIMAL_CELL = {
        "asset_key": "animal_cell_diagram", "canonical_key": "animal",
        "description": ("An educational diagram of an animal cell showing the "
                        "nucleus, cytoplasm and cell membrane."),
        "subject": "biology", "grade": "k12", "curriculum": "generic",
        "topic": "animal cell", "concepts": ["cell"], "status": "approved",
        "asset_type": "visual", "local_cache_path": "/tmp/ac.png",
    }

    def test_the_residuals_look_like_a_contrast_and_it_is_admitted_anyway(self):
        """Not luck: the gate is which path admitted, not the token counts."""
        q = vl.guard_tokens("cell_diagram")
        r = vl.guard_tokens("animal_cell_diagram") | vl.guard_tokens("animal")
        assert vl._unmatched(q, r) == {"cell", "diagram"}
        assert vl._unmatched(r, q) == {"animal"}
        assert all_noise("cell_diagram")
        assert vl.key_guard_ok("cell_diagram", self._ANIMAL_CELL) is True

    def test_and_the_library_still_serves_it(self, tmp_path, monkeypatch):
        _library(monkeypatch, tmp_path, self._ANIMAL_CELL)
        assert vl.find("cell_diagram",
                       "An educational diagram of an animal cell showing the "
                       "nucleus, cytoplasm and cell membrane") is not None

    def test_an_all_noise_row_is_abstained_on_too(self):
        assert vl.key_guard_ok("plant_cell", {
            "asset_key": "cell_diagram",
            "canonical_key": "cell_diagram"}) is True

    def test_the_abstention_still_refuses_what_it_always_did(self):
        assert vl.key_guard_ok("cell_diagram", {
            "asset_key": "volcano_diagram",
            "canonical_key": "volcano"}) is False


class TestWhyDidXNotMatchYIsAnswerableFromTheLog:
    """One line per DECISION, carrying the score and the REAL reason.

    Two things were wrong with logging the contrast refusal inside the guard.
    Volume: `key_guard_ok` is a predicate inside `best_match`'s `_eligible`
    filter, so it runs once per row of the local set and once per row of the
    remote set, and `_decide` performs four such scans per uncached asset —
    a measured mean of 4.75 refusals per scan against the live library, about
    760 lines for a 40-asset lesson, ~90 % of them about rows that never came
    within reach of the threshold, in the one stream an operator greps when a
    lesson fails. And accuracy: find() then re-tested the same row and printed
    a FIXED reason, "the keys share no concept token", which is the opposite
    of true for a contrast pair — the branch is only reachable when they share
    one. The last, summary-shaped, score-carrying line said the two keys were
    unrelated.
    """

    def _find_against(self, tmp_path, monkeypatch, row, key, prompt, caplog):
        import logging
        _library(monkeypatch, tmp_path, row)
        with caplog.at_level(logging.INFO, logger="shared.visual_library"):
            assert vl.find(key, prompt, min_score=0.58) is None
        return [r.getMessage() for r in caplog.records
                if "refused" in r.getMessage()]

    def test_find_logs_the_contrast_refusal_once_with_the_right_reason(
            self, tmp_path, monkeypatch, caplog):
        lines = self._find_against(
            tmp_path, monkeypatch, _CATALYST, "endothermic_energy_profile",
            _ENDOTHERMIC["description"], caplog)
        assert len(lines) == 1, lines
        line = lines[0]
        assert "endothermic_energy_profile" in line
        assert "catalyst_energy_profile" in line
        assert "endothermic" in line and "catalyst" in line
        assert "energy, profile" in line, \
            "the shared tokens too, so the refusal can be argued with"
        assert "share no concept token" not in line, \
            "they share two; that sentence is what made the log misleading"
        assert "(score 0." in line, "the line an operator greps carries a score"

    def test_the_prose_refusal_keeps_the_words_it_always_had(
            self, tmp_path, monkeypatch, caplog):
        """The reason is now the guard's, so the OTHER reasons must survive
        the change intact — this is the line that has been in the log since
        the key guard landed."""
        volcano = {
            "asset_key": "volcano_cross_section",
            "canonical_key": "cross_section_volcano",
            "description": ("A volcano cut in half showing the magma chamber, "
                            "the central vent and the cone"),
            "subject": "geography", "grade": "k12", "curriculum": "generic",
            "topic": "volcano", "concepts": [], "status": "approved",
            "asset_type": "visual", "local_cache_path": "/tmp/v.png"}
        lines = self._find_against(
            tmp_path, monkeypatch, volcano, "magma_chamber_labels",
            "A volcano cut in half showing the magma chamber, the central "
            "vent and the cone", caplog)
        assert len(lines) == 1, lines
        assert "share no concept token" in lines[0], lines

    def test_the_guard_itself_stays_quiet_while_it_filters(self, caplog):
        """The predicate runs once per candidate row. Scanned over a library,
        it must not put a line in the stream for every one of them."""
        import logging
        rows = [{"asset_key": f"{w}_energy_profile",
                 "canonical_key": f"{w}_energy_profile"}
                for w in ("catalyst", "exothermic", "activation", "reaction",
                          "bond", "combustion", "neutralisation", "fuel")]
        with caplog.at_level(logging.INFO, logger="shared.visual_library"):
            refused = [r for r in rows
                       if not vl.key_guard_ok("endothermic_energy_profile", r)]
        assert len(refused) == len(rows), "all eight are contrasts"
        assert [r.getMessage() for r in caplog.records] == [], \
            "a whole-library filter must not log per candidate"

    def test_but_the_reason_is_still_available_to_whoever_decides(self):
        why = vl._guard_refusal("endothermic_energy_profile", _CATALYST)
        assert why and "endothermic" in why and "catalyst" in why
        assert vl._guard_refusal("endothermic_energy_profile", {
            "asset_key": "endothermic_energy_profile",
            "canonical_key": "endothermic_energy_profile"}) is None

    def test_an_admitted_pair_is_not_logged_as_a_refusal(self, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="shared.visual_library"):
            assert vl.key_guard_ok("volcano_cross_section", {
                "asset_key": "composite_volcano_cross_section",
                "canonical_key": "composite_cross_section_volcano"}) is True
        assert not any("denies" in r.getMessage() for r in caplog.records)
