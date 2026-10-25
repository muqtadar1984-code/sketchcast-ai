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


def test_an_observation_task_is_required_when_photos_exist():
    xs = [_x()]
    f = V.check_exercises(xs, {"claim:a"}, set(), {"7Bs.01"}, photo_ids={"asset:p1", "asset:p2"})
    assert any(c["check"] == "observation" for c in f)
