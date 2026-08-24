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


class TestMeasuredPartMap:
    """The part count stops being a guess as soon as the chapter is transcribed.

    A scanned book has no text at index time, so its map is inferred from page
    count at EST_WORDS_PER_PAGE = 250 — while a real scanned, illustrated
    textbook page measures ~145 words. The estimate OVER-OFFERS: on the founder's
    Cambridge book it advertised 44 parts against 29 buildable, and a teacher
    clicking part 8 of a 5-part chapter got "part 8 does not exist. If the book's
    chapters changed, re-index it" — advice that cannot fix an estimate.
    """

    SAMPLE = ("the cell wall controls what enters and leaves a plant cell while the "
              "membrane stays flexible and thin under most conditions ")

    def _chapter(self, words, title="Forces and energy"):
        t = self.SAMPLE * (words // len(self.SAMPLE.split()) + 2)
        return {"title": title, "num": 2, "start_page": 69, "end_page": 126,
                "sections": [{"section_title": "Content", "section_type": "body",
                              "content": " ".join(t.split()[:words]),
                              "page_num": 69, "subsections": []}]}

    def test_it_agrees_with_what_generation_will_build(self):
        """THE property. Generation validates part_ref against
        build_chapter_parts(chapter) on this same dict, so a map measured here
        can never advertise a part that does not exist."""
        from agent2_analysis.analyzer import build_chapter_parts
        from shared.part_label import measured_parts_for

        for pages in (32, 28, 58, 26, 38, 43, 30, 30, 28):
            ch = self._chapter(pages * 145)
            measured = measured_parts_for(ch, [{"words": 999, "estimated": True}] * 9)
            assert measured is not None
            assert len(measured) == len(build_chapter_parts(ch)), f"{pages}pp disagrees"

    def test_it_replaces_the_over_offering_estimate(self):
        from shared.part_label import measured_parts_for

        # 58-page chapter: the estimate offered 8 parts (58*250/1950); the real
        # text at 145 w/page builds 5.
        estimate = [{"titles": [], "words": 1812, "estimated": True} for _ in range(8)]
        measured = measured_parts_for(self._chapter(58 * 145), estimate)
        assert measured is not None and len(measured) < len(estimate)
        assert all("estimated" not in p for p in measured), "the estimate flag must be gone"
        assert all(p["words"] > 0 for p in measured)

    def test_no_part_exceeds_the_fifteen_minute_budget(self):
        from agent2_analysis.analyzer import MAX_PART_WORDS
        from shared.part_label import measured_parts_for

        for p in measured_parts_for(self._chapter(58 * 145), []):
            assert p["words"] <= MAX_PART_WORDS

    def test_a_suspect_page_range_keeps_its_low_confidence(self):
        """Measuring words INSIDE a suspect range does not make the range right.
        The count becomes real; its provenance does not."""
        from shared.part_label import measured_parts_for

        gated = [{"titles": [], "words": 1812, "estimated": True, "low_confidence": True}] * 8
        measured = measured_parts_for(self._chapter(58 * 145), gated)
        assert all(p.get("low_confidence") for p in measured)
        # …and a healthy book gains no such marker.
        clean = measured_parts_for(self._chapter(58 * 145),
                                   [{"titles": [], "words": 1812, "estimated": True}] * 8)
        assert all("low_confidence" not in p for p in clean)

    def test_it_is_a_no_op_once_already_measured(self):
        """Re-writing an identical map on every generation would churn the row
        and log noise for nothing."""
        from shared.part_label import measured_parts_for

        ch = self._chapter(58 * 145)
        first = measured_parts_for(ch, [])
        assert first is not None
        assert measured_parts_for(ch, first) is None

    def test_an_empty_chapter_leaves_the_stored_map_alone(self):
        """Returning [] here would erase a real map because OCR came back blank."""
        from shared.part_label import measured_parts_for

        blank = {"title": "X", "num": 0, "sections": [
            {"section_title": "Content", "section_type": "body",
             "content": "", "page_num": 0, "subsections": []}]}
        assert measured_parts_for(blank, [{"words": 500, "estimated": True}]) is None
        assert measured_parts_for({"title": "X", "num": 0, "sections": []}, []) is None

    def test_the_placeholder_heading_never_reaches_storage(self):
        """The OCR path names every section the literal "Content"; stored, the
        app would render it as the part's NAME instead of "Part 2 of 5"."""
        from shared.part_label import measured_parts_for

        for p in measured_parts_for(self._chapter(58 * 145), []):
            assert p["titles"] == []

    def test_a_failed_transcription_cannot_erase_a_good_map(self):
        """Caught by the test above before it shipped: build_chapter_parts
        prefixes every unit with "## <title>", so a chapter whose OCR came back
        EMPTY still produced one part of two words ("##", "Content") — enough to
        look like a measurement and replace a good 8-part map with a single junk
        row. The check asks the chapter's own CONTENT, never the chunker."""
        from shared.part_label import measured_parts_for

        good = [{"titles": [], "words": 1812, "estimated": True} for _ in range(8)]
        for content in ("", "   ", "\n\n", "Chapter 3"):   # blank / near-blank OCR
            ch = {"title": "Forces and energy", "num": 2, "sections": [
                {"section_title": "Content", "section_type": "body",
                 "content": content, "page_num": 69, "subsections": []}]}
            assert measured_parts_for(ch, good) is None, repr(content)

    def test_the_transcriber_reports_what_it_actually_read(self):
        """The bounds check catches the runaway clamp. It CANNOT catch the other
        short read: on an API failure mid-loop chapter_text_vision returns the
        chunks it already has rather than losing them, so a half-read chapter
        looks complete. Only the transcriber knows, so it reports."""
        import agent1_ingestion.vision_chapters as vc

        rendered = {"n": 0}

        def fake_render(pdf, pages, width, out_dir):
            pages = list(pages)
            rendered["n"] += len(pages)
            return [out_dir / f"p{p:04d}.jpg" for p in pages]

        class DyingClient:
            def __init__(self, die_after): self.calls, self.die_after = 0, die_after
            def transcribe_images(self, paths, prompt, max_tokens=8000):
                self.calls += 1
                if self.calls > self.die_after:
                    raise RuntimeError("rate limited")
                return {"text": "word " * 400, "usage": {}}

        import pathlib
        for p in (vc.Path,):  # keep Path import used
            assert p is pathlib.Path

        vc._render_pages = fake_render
        report: dict = {}
        text = vc.chapter_text_vision("x.pdf", 0, 29, DyingClient(die_after=2), report=report)
        assert report["pages_requested"] == 30
        assert 0 < report["pages_done"] < 30, report
        assert text, "partial text is still returned — it must not be thrown away"
