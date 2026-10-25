"""Stage E deterministic gates (founder's Gate D review of Biology ch 1, 2026-10-25)."""
from publishing.manuscript import dossier as D
from publishing.manuscript import render as R
from publishing.manuscript import validate as V

PAIRS = [{"left": "unicellular", "right": "one cell does every job"},
         {"left": "multicellular", "right": "many cells share the work"},
         {"left": "prokaryotic", "right": "DNA lies in the cytoplasm"},
         {"left": "eukaryotic", "right": "DNA inside a nucleus"}]


def _x(**kw):
    base = {"xid": "x4", "item_type": "match", "stem": "Match each term to its description.", "pairs": PAIRS,
            "answer": "see key", "worked_solution": "…", "marks": 4,
            "marking_scheme": [{"point": "one mark for each correct pairing of term and description", "marks": 4}]}
    return {**base, **kw}


def test_a_match_that_prints_its_own_answers_fails():
    leaked = _x(stem="Match: unicellular — one cell does every job; multicellular — many cells share the work; "
                     "prokaryotic — DNA lies in the cytoplasm.")
    assert any("given away" in f["detail"] for f in V.check_assessment(leaked))
    assert not V.check_assessment(_x())


def test_a_match_without_structured_pairs_fails():
    assert any("structured pairs" in f["detail"] for f in V.check_assessment(_x(pairs=None)))


def test_the_book_scrambles_column_b_and_keys_it():
    order = R._scramble("x4", 4)
    assert sorted(order) == [0, 1, 2, 3] and all(order[i] != i for i in range(4)), "no pair beside its own answer"
    html, key = R._match(_x())
    assert key.count("–") == 4 and R._scramble("x4", 4) == order, "reproducible"


def test_a_mark_point_must_say_what_earns_it():
    bad = _x(item_type="short_answer", pairs=None, marking_scheme=[{"point": "difference 1", "marks": 1}], marks=1)
    assert any("does not say" in f["detail"] for f in V.check_assessment(bad))


def test_an_mcq_answer_must_be_an_option():
    ok = _x(item_type="mcq", pairs=None, options=["The organ", "The cell"], answer="B")
    bad = _x(item_type="mcq", pairs=None, options=["The organ", "The cell"], answer="The atom")
    assert not V.check_assessment(ok) and V.check_assessment(bad)


def test_a_generated_image_never_poses_as_a_photo():
    fake = {"provenance": "generated:gemini-3-pro-image", "asset_key": "amoeba_seen_under__photo__deck",
            "description": "Amoeba seen under a microscope"}
    real = {"provenance": "flickr", "asset_key": "amoeba_microscope_seen_under__micrograph__deck",
            "source": '{"url": "https://www.flickr.com/x", "licence": "CC BY 2.0", "author": "euthman"}'}
    nc = {**real, "source": '{"licence": "CC BY-NC 2.0", "author": "x"}'}
    drawing = {"provenance": "generated", "asset_key": "amoeba", "description": "A simple line diagram of an amoeba"}
    assert D.usable_library_asset(fake)[0] is False
    assert D.usable_library_asset(real)[:3:2] == (True, "photo")
    assert D.usable_library_asset(nc)[0] is False, "NC is never printable (D7)"
    assert D.usable_library_asset(drawing) == (True, None, "illustration")


def test_a_sort_task_must_be_well_formed():
    good = _x(item_type="sort", pairs=None, categories=["unicellular", "multicellular"],
              items=[{"text": t, "category": c} for t, c in (("Amoeba", "unicellular"), ("human", "multicellular"),
                                                             ("bacterium", "unicellular"), ("pea plant", "multicellular"))])
    bad = _x(item_type="sort", pairs=None, categories=["unicellular"], items=[{"text": "x", "category": "y"}])
    assert not V.check_assessment(good) and V.check_assessment(bad)


def test_an_observation_task_says_where_each_sample_came_from():
    x = _x(item_type="short_answer", pairs=None, figure_refs=["asset:a", "asset:b"], figure_context={"A": "pond water"})
    assert any("figure_context" in f["detail"] for f in V.check_assessment(x))


def test_true_false_items_are_limited_and_patterns_refused():
    tf = [_x(xid=f"x{i}", item_type="true_false", pairs=None, answer="True") for i in range(3)]
    f = V.check_exercises(tf, set(), set(), {"7Bs.01"})
    assert any("true/false items" in c["detail"] for c in f) and any("same answer" in c["detail"] for c in f)


def test_a_think_box_carries_its_answer_and_appears_once():
    s = {"sid": "s1", "blocks": [{"type": "think", "text": "Why?", "source_refs": ["claim:a"]},
                                 {"type": "think", "text": "And?", "answer": "Because", "source_refs": ["claim:a"]}]}
    f = V.check_sections([s], {"claim:a"}, set())
    assert any("answer" in c["detail"] for c in f) and any("more than one think" in c["detail"] for c in f)


def test_identity_flags_medical_sources_and_licence_links_resolve():
    from publishing.manuscript import identity as I
    assert I.MEDICAL.search("Amebic colitis (H&E)") and I.MEDICAL.search("Red blood cells infected with malaria")
    assert not I.MEDICAL.search("Paramecium bursaria - 400x")
    assert R.licence_url("CC BY 2.0") == "https://creativecommons.org/licenses/by/2.0/"
    assert R.licence_url("CC0").startswith("https://creativecommons.org/publicdomain/zero")


def test_book_selection_prefers_photos_then_colour():
    assert D.representation({"provenance": "flickr"}) == "photo"
    assert D.representation({"provenance": "generated", "asset_key": "plant_cell__colour"}) == "colour_illustration"
    assert D.representation({"provenance": "generated", "asset_key": "plant_cell"}) == "whiteboard"
    assert D.REPRESENTATION_ORDER.index("photo") < D.REPRESENTATION_ORDER.index("whiteboard")


def test_an_observation_task_is_required_when_photos_exist():
    xs = [_x()]
    f = V.check_exercises(xs, {"claim:a"}, set(), {"7Bs.01"}, photo_ids={"asset:p1", "asset:p2"})
    assert any(c["check"] == "observation" for c in f)
