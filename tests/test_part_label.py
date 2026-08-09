"""Part labels — "all parts carry the chapter name and part number, not just
Part 1, Part 2".

The premise that a part had no title was wrong: the field is ``titles`` (plural)
and it WAS written — with ["Content"], the structurer's no-headings-found
placeholder, on 143 of the 304 parts in production. So every card read
"Content", and the stored string every non-Library surface renders read
"… · Part 3" with no total and no chapter anchor.
"""

from shared.part_label import clean_part_titles, part_heading, part_label

CHAPTER = "Feedback Loops and Delays"


class TestHeading:
    def test_the_content_placeholder_is_never_a_name(self):
        # 47% of production parts. No previous filter caught it.
        assert part_heading(["Content"], CHAPTER) is None
        for junk in ("Contents", "TEXT", "body", "Untitled", "Chapter", "section"):
            assert part_heading([junk], CHAPTER) is None, junk

    def test_a_real_heading_survives(self):
        assert part_heading(["Balancing loops"], CHAPTER) == "Balancing loops"

    def test_bare_numerals_are_dropped(self):
        assert part_heading(["3", "2.", "(4)", "Stocks and flows"], CHAPTER) == "Stocks and flows"

    def test_an_echo_of_the_chapter_name_is_dropped(self):
        assert part_heading(["  feedback loops AND delays "], CHAPTER) is None

    def test_the_first_survivor_wins(self):
        assert part_heading(["Content", "1", "Delays"], CHAPTER) == "Delays"

    def test_a_substring_match_is_not_a_placeholder(self):
        # Exact match only — a real heading must not be eaten.
        assert part_heading(["Content of the cell"], CHAPTER) == "Content of the cell"


class TestLabel:
    def test_the_chapter_name_always_travels_with_the_ordinal(self):
        assert part_label(CHAPTER, 3, 7) == f"{CHAPTER} · Part 3 of 7"

    def test_a_real_heading_is_appended_not_substituted(self):
        assert part_label(CHAPTER, 3, 7, ["Balancing loops"]) == (
            f"{CHAPTER} · Part 3 of 7 — Balancing loops"
        )

    def test_the_placeholder_degrades_to_the_ordinal(self):
        assert part_label(CHAPTER, 3, 22, ["Content"]) == f"{CHAPTER} · Part 3 of 22"

    def test_a_single_part_chapter_is_just_the_chapter(self):
        # "Part 1 of 1" is noise, and a re-index that shrinks a part map used
        # to emit exactly that.
        assert part_label(CHAPTER, 1, 1) == CHAPTER
        assert part_label(CHAPTER, 1, 1, ["Balancing loops"]) == CHAPTER
        assert part_label(CHAPTER) == CHAPTER

    def test_the_total_is_always_present(self):
        # Documents used to emit "· Part 3" with no total, so part 1's and
        # part 3's worksheets were indistinguishable.
        assert " of " in part_label(CHAPTER, 1, 4)

    def test_degenerate_input_never_raises(self):
        assert part_label("", None, None) == "Lesson"
        assert part_label(CHAPTER, "x", "y") == CHAPTER
        assert part_label(CHAPTER, 0, 5) == CHAPTER


class TestStoredTitles:
    def test_placeholders_never_reach_storage(self):
        assert clean_part_titles(["Content"], CHAPTER) == []

    def test_real_headings_are_kept_deduped_and_capped(self):
        titles = ["Stocks", "Stocks", "Flows", "Delays", "Loops"]
        assert clean_part_titles(titles, CHAPTER) == ["Stocks", "Flows", "Delays"]

    def test_empty_input_is_empty_output(self):
        assert clean_part_titles(None, CHAPTER) == []
        assert clean_part_titles([], CHAPTER) == []
