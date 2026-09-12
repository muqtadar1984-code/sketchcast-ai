"""The deck's own storyboard: what it makes, what it splits, what it omits.

Every fault pinned here was found by BUILDING the Cells deck and looking at
it. None of them raised, none of them failed a count, and all three would
have been projected in front of a class:

  * nine organelle definitions on one slide, the last three below the bottom
    edge, because the storyboard paginated in invented units while the
    renderer laid out in inches;
  * "Structures unique to plant cells:" alone on a slide with five inches of
    white space under it, its list overleaf;
  * the same sentence printed twice — once in the key-idea band and again as
    the first line of the body — because the article's claims are extracted
    FROM the body.
"""

from __future__ import annotations

from agent5_slides import metrics as mx
from agent5_slides.deck_storyboard import (CHECK, CLOSING, DIAGRAM, GLOSSARY,
                                           MISCONCEPTIONS, OBJECTIVES, SECTION,
                                           TITLE, WORKED, _paginate,
                                           drop_restatement, labels_per_slide,
                                           split_parts, storyboard, summarise)
from shared.lesson_model import (Figure, LessonModel, Section, from_analysis,
                                 from_article, parse_body)


def _fig(n: int, key: str = "f", big: int = 0) -> Figure:
    """A figure with `n` located parts; the first `big` are large."""
    regions, parts = {}, []
    for i in range(n):
        name = f"part{i}"
        side = 300 if i < big else 20
        regions[name] = [[i * 3, i * 3, i * 3 + side, i * 3 + side]]
        parts.append(name)
    f = Figure(key=key, caption="A caption", parts=parts, regions=regions,
               w=1000, h=800)
    f.png = None
    return f


class TestHowManyLabelsFitBeforeADiagramMustSplit:
    def test_capacity_is_derived_from_the_font_not_hardcoded(self):
        """Raising the label size must REDUCE the count, never overflow."""
        assert labels_per_slide(9.0) >= labels_per_slide(12.0) >= labels_per_slide(24.0)

    def test_it_never_returns_a_number_that_cannot_be_drawn(self):
        for pt in (9.0, 12.0, 14.0, 18.0, 28.0, 44.0):
            n = labels_per_slide(pt)
            per_side = -(-n // 2)
            pitch = (pt * 1.22 * 2) / 72.0 + 0.07
            assert per_side * pitch <= mx.BODY_H_IN + 0.01, (pt, n)

    def test_clutter_caps_the_arithmetic(self):
        """Ten leaders a side overlap nothing and are still unreadable, so the
        cap is a judgement the arithmetic cannot make."""
        assert labels_per_slide(12.0) == 12


class TestSplittingAFigureAcrossSlides:
    def test_a_figure_that_fits_is_one_slide(self):
        assert len(split_parts(_fig(9), 12)) == 1

    def test_a_crowded_figure_splits(self):
        assert len(split_parts(_fig(14), 12)) == 2

    def test_the_groups_are_balanced_not_filled(self):
        """14 at a capacity of 12 is 7 and 7. Filling would give 12 and 2, and
        a slide holding two labels beside a full diagram looks broken."""
        groups = split_parts(_fig(14), 12)
        assert [len(g) for g in groups] == [7, 7]

    def test_nothing_is_lost_and_nothing_is_repeated(self):
        fig = _fig(25)
        flat = [p for g in split_parts(fig, 12) for p in g]
        assert sorted(flat) == sorted(fig.located())
        assert len(flat) == len(set(flat))

    def test_the_big_structures_come_first(self):
        """Teaching order and subject-blind order happen to agree: a learner
        who has not found the cell membrane cannot place a ribosome inside it."""
        groups = split_parts(_fig(14, big=4), 12)
        assert set(groups[0][:4]) == {"part0", "part1", "part2", "part3"}

    def test_a_figure_whose_parts_are_all_unlocated_yields_nothing(self):
        fig = _fig(3)
        fig.regions = {}
        assert split_parts(fig, 12) == []


class TestPaginationMeasuresWhatTheRendererDraws:
    LONG = ("Endoplasmic Reticulum (ER): A network of membranes that forms sacs "
            "and tubules throughout the cytoplasm. It plays a role in protein "
            "and lipid synthesis. There are two types: Rough ER and Smooth ER.")

    def _fits(self, pages, budget=mx.BODY_H_IN):
        for pg in pages:
            assert sum(mx.block_height_in(b) for b in pg) <= budget + 0.01, pg

    def test_a_long_list_splits_instead_of_running_off_the_slide(self):
        """The live fault: nine organelle definitions, three of them below the
        bottom edge of a slide a teacher was about to project."""
        blocks = [{"kind": "list", "items": [self.LONG] * 9}]
        pages = _paginate(blocks, mx.BODY_H_IN)
        assert len(pages) > 1
        self._fits(pages)

    def test_no_item_is_lost_when_a_list_splits(self):
        items = [f"{i}: {self.LONG}" for i in range(9)]
        pages = _paginate([{"kind": "list", "items": items}], mx.BODY_H_IN)
        got = [it for pg in pages for b in pg for it in b["items"]]
        assert got == items

    def test_a_lead_in_is_never_stranded_from_its_list(self):
        """"Structures unique to plant cells:" alone on a slide, its list
        overleaf. A block ending in a colon keeps its successor's first item."""
        blocks = [{"kind": "para", "text": "Filler. " * 90},
                  {"kind": "para", "text": "Structures unique to plant cells:"},
                  {"kind": "list", "items": [self.LONG] * 6}]
        pages = _paginate(blocks, mx.BODY_H_IN)
        for pg in pages:
            if pg and pg[-1].get("text", "").endswith(":"):
                raise AssertionError("a colon lead-in ended a page")

    def test_a_lead_in_reserves_a_TABLE_whole_because_it_cannot_split(self):
        """Reserving a nominal inch left "Differences between plant and animal
        cells:" at the foot of one page with its table on the next."""
        blocks = [{"kind": "para", "text": "Filler. " * 80},
                  {"kind": "para", "text": "Differences between plant and animal cells:"},
                  {"kind": "table", "header": ["Feature", "Plant", "Animal"],
                   "rows": [["a", "b", "c"]] * 7}]
        pages = _paginate(blocks, mx.BODY_H_IN)
        for i, pg in enumerate(pages):
            if pg and pg[-1].get("text", "").endswith(":"):
                raise AssertionError(f"lead-in stranded on page {i}")

    def test_every_page_fits_for_a_mixed_body(self):
        blocks = [{"kind": "heading", "text": "A heading"},
                  {"kind": "para", "text": "Prose. " * 60},
                  {"kind": "list", "items": [self.LONG] * 5},
                  {"kind": "table", "header": ["a", "b"], "rows": [["x", "y"]] * 6},
                  {"kind": "para", "text": "A closing sentence."}]
        self._fits(_paginate(blocks, mx.BODY_H_IN))

    def test_an_empty_body_is_one_empty_page_not_a_crash(self):
        assert _paginate([], mx.BODY_H_IN) == [[]]


class TestTheKeyIdeaIsNotSaidTwice:
    IDEA = "The cell is the fundamental structural and functional unit of all known living organisms."

    def test_a_verbatim_leading_sentence_is_dropped_from_the_body(self):
        blocks = [{"kind": "para", "text": self.IDEA + " All living things are made of cells."}]
        got = drop_restatement(blocks, self.IDEA)
        assert got[0]["text"] == "All living things are made of cells."

    def test_a_paragraph_that_is_ONLY_the_restatement_goes_entirely(self):
        assert drop_restatement([{"kind": "para", "text": self.IDEA}], self.IDEA) == []

    def test_a_merely_similar_sentence_is_kept(self):
        """A claim that resembles the prose is a genuine summary; both earn
        their place. Only an exact restatement is removed."""
        blocks = [{"kind": "para", "text": "Cells are the basic unit of every living thing."}]
        assert drop_restatement(blocks, self.IDEA) == blocks

    def test_no_key_idea_changes_nothing(self):
        blocks = [{"kind": "para", "text": self.IDEA}]
        assert drop_restatement(blocks, "") == blocks


class TestWhatTheDeckContains:
    def _model(self, **over) -> LessonModel:
        m = LessonModel(title="Cells")
        m.objectives = ["Identify the cell as the basic unit of life."]
        m.sections = [Section(id="s1", heading="Structure", body_md="Some prose.",
                              figure_keys=["f"], key_idea="Cells are the unit of life.")]
        m.glossary = [("cell", "the basic unit of life")]
        m.misconceptions = [("all cells are the same size", "they vary hugely")]
        m.worked_examples = [("Count the cells", "1. Do this.")]
        for k, v in over.items():
            setattr(m, k, v)
        return m

    def test_the_article_path_produces_every_kind(self):
        kinds = summarise(storyboard(self._model()))
        for k in (TITLE, OBJECTIVES, SECTION, MISCONCEPTIONS, WORKED, GLOSSARY, CLOSING):
            assert kinds.get(k), k

    def test_an_empty_list_omits_its_slide_rather_than_rendering_a_hollow_one(self):
        """The fail-over-degrade rule at deck scale. A teacher's deck is
        SHORTER than a catalogue lesson, not the same length with holes."""
        kinds = summarise(storyboard(self._model(
            objectives=[], misconceptions=[], worked_examples=[], glossary=[])))
        for k in (OBJECTIVES, MISCONCEPTIONS, WORKED, GLOSSARY):
            assert k not in kinds
        assert kinds[TITLE] == 1 and kinds[CLOSING] == 1

    def test_a_deck_always_opens_and_closes(self):
        slides = storyboard(LessonModel(title="Empty"))
        assert slides[0].kind == TITLE and slides[-1].kind == CLOSING

    def test_the_key_idea_leads_a_section_and_is_not_repeated_on_its_continuation(self):
        m = self._model()
        m.sections[0].body_md = "Prose. " * 400
        secs = [s for s in storyboard(m) if s.kind == SECTION]
        assert len(secs) > 1
        assert secs[0].key_idea and not any(s.key_idea for s in secs[1:])
        assert all(s.continued for s in secs[1:])


class TestTheTeacherPathDegradesByOmission:
    ANALYSIS = {"concepts": [
        {"name": "Photosynthesis", "definition": "How plants make food from light.",
         "importance": "foundational"},
        {"name": "Chloroplast", "definition": "The organelle where it happens."},
        {"name": "Chloroplast", "definition": "A duplicate that must not appear twice."},
        {"name": "", "definition": "no name, dropped"}]}
    SCRIPT = {"episodes": [{"episode_title": "Photosynthesis", "segments": [
        {"segment_id": "s001", "slide_heading": "What plants need",
         "slide_points": ["Plants need light.", "They also need water."],
         "text": "Let us begin with what a plant needs."}]}]}

    def test_the_glossary_the_deck_has_been_throwing_away(self):
        m = from_analysis(self.ANALYSIS, self.SCRIPT)
        assert ("Photosynthesis", "How plants make food from light.") in m.glossary
        assert len(m.glossary) == 2, "duplicates and nameless concepts must not land"

    def test_objectives_are_NEVER_invented_from_concept_names(self):
        """"Cell membrane" -> "Explain the cell membrane" is a teaching
        objective the curriculum never set, printed under a school's name."""
        m = from_analysis(self.ANALYSIS, self.SCRIPT)
        assert m.objectives == []
        assert OBJECTIVES not in summarise(storyboard(m))

    def test_it_still_produces_a_usable_deck(self):
        kinds = summarise(storyboard(from_analysis(self.ANALYSIS, self.SCRIPT)))
        assert kinds[TITLE] == 1 and kinds[GLOSSARY] == 1 and kinds[CLOSING] == 1
        assert kinds.get(SECTION)

    def test_no_diagram_or_check_slide_without_artwork(self):
        kinds = summarise(storyboard(from_analysis(self.ANALYSIS, self.SCRIPT)))
        assert DIAGRAM not in kinds and CHECK not in kinds

    def test_the_narration_survives_into_the_notes(self):
        m = from_analysis(self.ANALYSIS, self.SCRIPT)
        assert m.sections[0].narration.startswith("Let us begin")


class TestMarkdownOnlyAsFarAsASlideNeeds:
    def test_a_table_is_recognised_and_becomes_a_table(self):
        md = ("| Feature | Plant | Animal |\n|---|---|---|\n"
              "| Cell wall | Present | Absent |\n")
        blocks = parse_body(md)
        assert blocks[0]["kind"] == "table"
        assert blocks[0]["header"] == ["Feature", "Plant", "Animal"]
        assert blocks[0]["rows"] == [["Cell wall", "Present", "Absent"]]

    def test_a_pipe_in_prose_is_not_a_table(self):
        """"either | or" in a sentence must not swallow the paragraph — the
        separator row on the NEXT line is what makes a table."""
        blocks = parse_body("A choice of either | or, written in prose.")
        assert [b["kind"] for b in blocks] == ["para"]

    def test_bullets_and_numbers_both_become_lists(self):
        assert parse_body("- one\n- two")[0]["items"] == ["one", "two"]
        assert parse_body("1. one\n2. two")[0]["items"] == ["one", "two"]

    def test_an_enumeration_on_one_line_is_a_sentence_not_a_list(self):
        """A live worked example: "1. Cell wall, 2. Cell membrane, 3.
        Cytoplasm" rendered as a bullet reading "Cell wall, 2. Cell
        membrane…" with its own "1." eaten."""
        blocks = parse_body("1. Cell wall, 2. Cell membrane, 3. Cytoplasm, 4. Vacuole.")
        assert [b["kind"] for b in blocks] == ["para"]
        assert blocks[0]["text"].startswith("1. Cell wall")

    def test_blank_lines_separate_paragraphs(self):
        assert [b["kind"] for b in parse_body("First para.\n\nSecond para.")] == ["para", "para"]


class TestFigureReadiness:
    def test_regions_without_a_measured_frame_are_not_annotatable(self):
        """`vision.w`/`h` are the frame the boxes were MEASURED in. Without
        them a box is numbers with no scale and every label lands arbitrarily."""
        f = _fig(4)
        f.png = __import__("pathlib").Path(__file__)   # a file that exists
        assert f.annotatable
        f.w = 0
        assert not f.annotatable

    def test_only_parts_the_artwork_can_place_are_offered(self):
        f = _fig(3)
        f.parts = f.parts + ["a part nobody drew"]
        assert "a part nobody drew" not in f.located()


class TestADiagramSlideTitle:
    """The Materials deck titled a slide "A comparison of the three states of
    matter showing three containers side-by-side representing solid, liquid,
    and gas par" — a library row's description sentence, clipped by the
    renderer at 120 characters, mid-word. A short caption names a slide; a
    long one is a subtitle under the section's own heading."""

    def _deck(self, caption: str):
        f = _fig(3, key="states")
        f.png = __import__("pathlib").Path(__file__)
        f.caption = caption
        sec = Section(id="s1", heading="The Mystery of Materials", body_md="Prose.", figure_keys=["states"])
        m = LessonModel(title="Materials", sections=[sec], figures={"states": f})
        return [s for s in storyboard(m) if s.kind == DIAGRAM]

    def test_a_short_caption_is_the_title(self):
        (s,) = self._deck("The three states of matter")
        assert s.heading == "The three states of matter"
        assert s.kicker == "The Mystery of Materials"
        assert s.subtitle == "", "a subtitle that repeats the title is noise"

    def test_a_long_caption_becomes_the_subtitle(self):
        long = ("A comparison of the three states of matter showing three containers "
                "side-by-side representing solid, liquid, and gas particle arrangements.")
        (s,) = self._deck(long)
        assert s.heading == "The Mystery of Materials"
        assert s.subtitle == long
        assert s.kicker == "", "the kicker would repeat the title"

    def test_no_caption_at_all_still_has_a_title(self):
        (s,) = self._deck("")
        assert s.heading == "The Mystery of Materials" and s.subtitle == ""
