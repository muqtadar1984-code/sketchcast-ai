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


def test_every_book_gets_a_theme_from_data():
    from publishing.manuscript import theme as T
    assert T.for_work("cells-structure-function-organisation")["name"] == "life"
    assert T.for_work("angles-triangles-geometric-reasoning")["name"] == "geometry"
    assert T.for_work("no-such-book")["name"]                      # falls back to the default, never fails
    assert T.motif_svg(T.for_work("angles-triangles-geometric-reasoning")).startswith("<svg")


def test_image_style_is_chosen_by_genre_not_subject():
    from publishing.manuscript import book_assets as B
    s = B.styles()
    assert {"natural_history", "scientific_3d", "apparatus", "process", "never_generate"} <= set(s["genres"])
    p = B.prompt_for("a ripple tank", "apparatus", {"image_palette": "deep blues"})
    assert s["genres"]["apparatus"]["style"] in p and "deep blues" in p and "no text" in p.lower()


def test_page_design_is_token_driven_and_print_has_bleed():
    from publishing.manuscript.theme import backgrounds, for_work, page, page_css
    t = for_work("cells-structure-function-organisation")
    assert page(t)["paper"] == "#faf6ec"
    digital, printed = page_css(t, 1, "T", "W"), page_css(t, 1, "T", "W", "print")
    assert "size: 210mm 297mm" in digital and "size: 216mm 303mm" in printed
    assert "url(" not in digital          # Edge cannot print @page images: art is underlaid instead
    art = backgrounds(t, 3, "digital")
    assert set(art) == {"first", "verso", "recto"} and b"#faf6ec" in art["recto"]
    # the tab steps down by chapter
    assert backgrounds(t, 1)["recto"] != art["recto"]


def test_unconfigured_theme_falls_back_to_plain_page():
    from publishing.manuscript.theme import page
    p = page({"brand": "#000"})
    assert p["paper"] == "#ffffff" and p["tab"] is None


def test_opener_key_words_are_drawn_from_the_chapter_only():
    from publishing.manuscript.render import _key_words
    doc = {"sections": [{"blocks": [
        {"type": "paragraph", "text": "A **cell** is small. Notice **typically**."},
        {"type": "key_term", "term": "unicellular organism", "text": "x"},
        {"type": "paragraph", "text": "**unicellular** again, and **cells**."}]}]}
    out = _key_words(doc)
    assert "cell" in out and "unicellular organism" in out
    assert "typically" not in out and out.count("<span>") == 2


def test_expansion_keeps_every_approved_block_verbatim():
    from publishing.manuscript.validate import check_preserved
    old = [{"sid": "s1", "heading": "H", "blocks": [{"type": "paragraph", "text": "A."},
                                                     {"type": "key_idea", "text": "B."}]}]
    grown = [{"sid": "s1", "heading": "H", "blocks": [{"type": "paragraph", "text": "A."},
                                                       {"type": "paragraph", "text": "New."},
                                                       {"type": "key_idea", "text": "B."}]}]
    assert check_preserved(old, grown) == []
    reworded = [{"sid": "s1", "heading": "H", "blocks": [{"type": "paragraph", "text": "A, reworded."},
                                                          {"type": "key_idea", "text": "B."}]}]
    assert [f["check"] for f in check_preserved(old, reworded)] == ["preserved"]
    swapped = [{"sid": "s1", "heading": "H", "blocks": list(reversed(old[0]["blocks"]))}]
    assert check_preserved(old, swapped)


def test_restore_locked_undoes_a_reworded_or_dropped_block():
    from publishing.manuscript.validate import check_preserved, restore_locked
    old = [{"sid": "s1", "heading": "H", "blocks": [
        {"type": "figure", "ref": "asset:p", "caption": "A photograph of a Paramecium, made of one cell."},
        {"type": "paragraph", "text": "The photograph shows Paramecium, a microorganism that is only one cell."},
        {"type": "key_idea", "text": "Keep me."}]}]
    new = [{"sid": "s1", "heading": "H2", "blocks": [
        {"type": "paragraph", "text": "Added first."},
        {"type": "figure", "ref": "book:amoeba", "caption": "A drawing of an Amoeba, made of one cell."},
        {"type": "paragraph", "text": "The drawing shows Amoeba, a microorganism that is only one cell."},
        {"type": "paragraph", "text": "Added later."}]}]
    fixed = restore_locked(old, new)
    assert check_preserved(old, fixed) == []
    texts = [b.get("text") or b.get("ref") for b in fixed[0]["blocks"]]
    assert texts[0] == "Added first." and "Added later." in texts and "book:amoeba" not in texts


def test_questions_are_numbered_after_ordering_and_pictures_keep_the_questions_letters():
    from publishing.manuscript.render import _photo_row, question_numbers, question_order, renumber_text
    doc = {"exercises": [{"xid": "x1", "tier": "guided"}, {"xid": "x2", "tier": "challenge"},
                         {"xid": "x3", "tier": "guided"}, {"xid": "x4", "tier": "independent"}]}
    assert [x["xid"] for x in question_order(doc)] == ["x1", "x3", "x4", "x2"]
    assert question_numbers(doc) == {"x1": "X1", "x3": "X2", "x4": "X3", "x2": "X4"}
    assert renumber_text("as in X3 and X2", question_numbers(doc)) == "as in X2 and X4"
    figs = {"p": {"img": "data:,", "credit": None}}
    row = _photo_row(["p"], figs, {"C": "A sample scraped from a cheek"}, "Look at photograph C.")
    assert '<span class="pl">C</span>' in row and "A sample scraped" in row
    long = "A colour drawing (not a photograph) of a single leaf cut open in cross-section showing every layer"
    row = _photo_row(["p"], figs, {"A": long}, "Look at drawing A.")
    assert '<span class="ctx">' not in row and "alt=\"A colour drawing" in row
