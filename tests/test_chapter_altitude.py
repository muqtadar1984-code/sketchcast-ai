"""Chapter ALTITUDE — is a detected unit a chapter, or the Part that contains
the chapters?

The trigger: a 1,008-page university textbook whose PDF outline nests ~21
chapters under 7 Parts. ``_build_chapters_from_toc`` kept level 1 only, so the
book indexed as 8 "chapters" of 114-254 pages, each offering 19-28 lesson
parts, and 17 kits were generated against that map. Health called it
"excellent", because the only structure signal was ``n_chapters >= 3``.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent1_ingestion.extractor import DocItem, TOCItem
from agent1_ingestion.structurer import (_LABEL_RE, _MAX_SPAN_RATIO,
                                         _MAX_UNMAPPED_TAIL_SHARE,
                                         _better_family, _bounded_last_chapter,
                                         _build_chapters_from_toc,
                                         _chapters_plausible, _map_shape,
                                         _marker_number, _median,
                                         _page_text_stats, _pick_toc_depth,
                                         _ranges_valid, _repair_chapter_ranges,
                                         _toc_chapters_at_depth, _toc_is_usable,
                                         structure_book)

# Roughly one academic page of text — the shape checks estimate lesson-parts
# from the extracted volume, so page ranges alone are not enough.
_PAGE_TEXT = (
    "Systems thinking asks us to look past the events we notice to the structure "
    "that keeps producing them. In this section we build a stock and flow model of "
    "a simple supply chain, run it, and compare what it does with what the managers "
    "in the case expected it to do. The gap between the two is the lesson. "
) * 9


def _items(total_pages: int) -> list[DocItem]:
    return [DocItem(item_type="paragraph", text=_PAGE_TEXT, page_num=p, level=0)
            for p in range(total_pages)]


def _extraction(toc, total_pages, items=None):
    return SimpleNamespace(
        toc=toc, total_pages=total_pages,
        items=items if items is not None else _items(total_pages),
        readability_score=0.9, extraction_backend="pymupdf", markdown="",
    )


def _nested_toc() -> list[TOCItem]:
    """The failing book's shape: 7 Parts, ~3 chapters each, at outline depth 2."""
    toc = [TOCItem(level=1, title="BusinessDynamics", page_num=0)]
    page = 26
    for part in range(1, 8):
        toc.append(TOCItem(level=1, title=f"Part{chr(0x2160 + part - 1)} Perspective", page_num=page))
        for ch in range(3):
            toc.append(TOCItem(level=2, title=f"Chapter {part}.{ch} Learning in complex systems",
                               page_num=page + ch * 45))
        page += 140
    return toc


def _flat_toc(n: int, pages_each: int) -> list[TOCItem]:
    return [TOCItem(level=1, title=f"Unit {i + 1} Living things", page_num=i * pages_each)
            for i in range(n)]


# ── the depth chooser ────────────────────────────────────────────────────────

class TestTocDepth:
    def test_nested_outline_descends_to_the_chapter_level(self):
        toc = _nested_toc()
        total = 1008
        assert _pick_toc_depth(toc, total, None) == 2
        chapters = _build_chapters_from_toc(toc, total)
        # 21 chapters, not the 8 Parts that shipped.
        assert len(chapters) == 21
        assert all(c["end_page"] >= c["start_page"] for c in chapters)

    def test_a_level_2_chapter_stops_at_the_next_part(self):
        # The last chapter of Part 1 must not run on into Part 2.
        chapters = _build_chapters_from_toc(_nested_toc(), 1008)
        part2_start = next(t.page_num for t in _nested_toc()[1:] if t.level == 1 and t.page_num > 26)
        third = chapters[2]
        assert third["end_page"] < part2_start

    def test_a_legitimate_eight_chapter_book_still_indexes_as_eight(self):
        # 8 units over 240 pages = 30 pages each. Correct altitude; untouched.
        toc = _flat_toc(8, 30)
        assert _pick_toc_depth(toc, 240, None) == 1
        assert len(_build_chapters_from_toc(toc, 240)) == 8

    def test_a_small_book_is_never_judged(self):
        # 3 units over 60 pages: "is this a chapter or a Part?" is meaningless
        # below the size guard, so the map passes untouched.
        toc = _flat_toc(3, 20)
        assert _chapters_plausible(_build_chapters_from_toc(toc, 60), 60) is True

    def test_numbered_sections_are_never_chosen_as_chapters(self):
        # Descending into "3.1 / 3.2 / 3.3" would shatter the book — the
        # opposite failure to the one being fixed.
        toc = [TOCItem(level=1, title=f"Chapter {i + 1} Mechanics", page_num=i * 70)
               for i in range(6)]
        for i in range(6):
            for j in range(4):
                toc.append(TOCItem(level=2, title=f"{i + 1}.{j + 1} Forces and motion",
                                   page_num=i * 70 + j * 15))
        toc.sort(key=lambda t: (t.page_num, t.level))
        assert _pick_toc_depth(toc, 420, None) == 1

    def test_falls_back_to_depth_one_when_no_depth_validates(self):
        # A single flat outline that fails the envelope has nowhere to descend
        # to — keeping today's coarse map beats adopting an unvalidated one.
        toc = _flat_toc(5, 200)
        assert _pick_toc_depth(toc, 1000, None) == 1

    def test_two_level_one_bookmarks_on_one_page_do_not_force_a_descent(self):
        # REGRESSION. Preface and Acknowledgements bookmarked on the same page
        # is one of the commonest real outlines there is (so are Contents +
        # "How to use this book", and a Part title sharing a page with its
        # first chapter). The raw map _toc_chapters_at_depth builds for it has
        # end_page = start_page - 1 and a repeated start, both of which
        # _ranges_valid rejects — so judging the map UNREPAIRED failed depth 1
        # and descended to the section layer, turning 9 chapters into 27.
        toc = [TOCItem(level=1, title="Preface", page_num=4),
               TOCItem(level=1, title="Acknowledgements", page_num=4)]
        for i in range(9):
            start = 10 + i * 21
            toc.append(TOCItem(level=1, title=f"Chapter {i + 1} Forces and motion",
                               page_num=start))
            for j in range(3):  # a real, descendable level-2 layer
                toc.append(TOCItem(level=2, title=f"Forces at work {j}",
                                   page_num=start + 5 * (j + 1)))

        # The shape that used to cause it, asserted rather than assumed.
        assert _ranges_valid(_toc_chapters_at_depth(toc, 1, 200), 200) is False
        assert len(_toc_chapters_at_depth(toc, 2, 200)) == 27

        assert _pick_toc_depth(toc, 200, None) == 1
        chapters = _build_chapters_from_toc(toc, 200)
        assert len(chapters) == 10  # Preface + 9 chapters, never 27
        assert _ranges_valid(chapters, 200) is True

    def test_a_300_page_book_with_five_60_page_chapters_stays_at_five(self):
        # REGRESSION. An ordinary university textbook. The median-pages rule was
        # an absolute 55 with no corroboration, so this was called "the book's
        # parts, not its chapters" and thrown away — costing a paid detection
        # pass on a book whose outline was correct.
        toc = _flat_toc(5, 60)
        page_stats = _page_text_stats(_items(300))
        shape = _map_shape(_build_chapters_from_toc(toc, 300), 300, page_stats)
        assert shape["ok"] is True and shape["median_pages"] == 60
        assert _pick_toc_depth(toc, 300, page_stats) == 1

        book = structure_book(
            book_id="b", title="Thermodynamics", author="OUP", isbn=None,
            extraction=_extraction(toc, 300), images=[],
        )
        assert book.total_chapters == 5


# ── shape validation ─────────────────────────────────────────────────────────

class TestMapShape:
    def test_parts_masquerading_as_chapters_are_rejected(self):
        parts = [{"chapter_num": i, "title": f"Part {i}", "start_page": i * 126,
                  "end_page": (i + 1) * 126 - 1} for i in range(8)]
        shape = _map_shape(parts, 1008)
        assert shape["ok"] is False
        assert shape["median_pages"] == 126
        assert "parts, not its chapters" in shape["reason"]

    def test_a_real_chapter_map_passes(self):
        # Cambridge Primary Science Y7 — the largest LEGITIMATE median in the
        # production corpus (9 units / 344 pages).
        chapters = [{"chapter_num": i, "title": f"Unit {i + 1}", "start_page": i * 38,
                     "end_page": i * 38 + 37} for i in range(9)]
        assert _map_shape(chapters, 344)["ok"] is True

    def test_one_chapter_swallowing_the_book_is_rejected(self):
        # Production book 02d15db4: ten 7-page chapters and a 229-page tail.
        chapters = [{"chapter_num": i, "title": f"Skill {i}", "start_page": i * 7,
                     "end_page": i * 7 + 6} for i in range(10)]
        chapters.append({"chapter_num": 10, "title": "Compare & Evaluate",
                         "start_page": 72, "end_page": 300})
        shape = _map_shape(chapters, 301)
        assert shape["ok"] is False and "of the book" in shape["reason"]

    def test_sections_masquerading_as_chapters_are_rejected(self):
        sections = [{"chapter_num": i, "title": f"{i}.1 Topic", "start_page": i * 2,
                     "end_page": i * 2 + 1} for i in range(60)]
        assert _map_shape(sections, 240)["ok"] is False

    def test_estimator_stays_in_step_with_the_real_chunker(self):
        # The parts estimate models build_chapter_parts; if those budgets move
        # and this does not, the shape check silently measures nothing.
        from agent1_ingestion import structurer
        from agent2_analysis.analyzer import MAX_ANALYSIS_CHARS, MAX_PART_WORDS

        assert structurer._PART_CHARS == MAX_ANALYSIS_CHARS
        assert structurer._PART_WORDS == MAX_PART_WORDS


class TestLastChapterBound:
    def test_unbookmarked_back_matter_does_not_condemn_a_good_outline(self):
        # 10 units of 20 pages in a 300-page book: 100 pages of answers,
        # glossary and index carry no bookmark, so the final unit measures 40%
        # of the book and 6x its siblings. Cambridge/NCERT-style textbooks carry
        # exactly this. Judged UNBOUNDED the map was discarded; judged as it
        # will actually be stored — with the last-chapter bound applied — it is
        # a perfectly good 10-chapter map.
        chapters = _build_chapters_from_toc(_flat_toc(10, 20), 300)
        assert _map_shape(chapters, 300)["ok"] is False       # raw: rejected
        assert _chapters_plausible(chapters, 300) is True     # bounded: kept

    def test_the_bound_reports_the_pages_it_leaves_unmapped(self):
        # Silent content loss is worse than the over-long chapter it replaces:
        # unmapped pages are never extracted, analysed or taught.
        chapters = _build_chapters_from_toc(_flat_toc(10, 20), 300)
        bounded, unmapped = _bounded_last_chapter(chapters, 300)
        assert unmapped > 0
        assert bounded[-1]["end_page"] + 1 + unmapped == 300
        # …and the input is not mutated, so a plausibility probe cannot commit.
        assert chapters[-1]["end_page"] == 299

    def test_a_normal_tail_is_absorbed_with_nothing_dropped(self):
        chapters = _build_chapters_from_toc(_flat_toc(9, 38), 344)
        bounded, unmapped = _bounded_last_chapter(chapters, 344)
        assert unmapped == 0 and bounded[-1]["end_page"] == 343


class TestTruncatedMap:
    """REGRESSION. Measuring the BOUNDED map fixed one bug and opened another:
    the bound clips the last unit to max(3x median, median + 20), which is under
    _MAX_SPAN_RATIO by construction, so the span rule went blind to a map that
    stops a quarter of the way into the book. Such a map was ACCEPTED and
    silently truncated instead of falling through to the Claude/vision rescue.
    """

    def _stopped_early(self, n, span, total_pages=301):
        """A TOC of ``n`` units of ``span`` pages, then nothing: the classic
        shape of detection giving up. The final unit is elastic to the end of
        the book, exactly as _toc_chapters_at_depth builds it."""
        return _build_chapters_from_toc(
            [TOCItem(level=1, title=f"Skill {i + 1} Compare and evaluate",
                     page_num=i * span) for i in range(n)],
            total_pages,
        )

    def test_the_bound_alone_can_no_longer_hide_a_truncated_map(self):
        # Production shape: 11 units of 7 pages in a 301-page book. Measured
        # before this fix — raw shape "one unit is 77% of the book and 33x its
        # siblings", but _chapters_plausible True, 11 chapters stored over pages
        # 0-96 and 204 of 301 pages dropped.
        chapters = self._stopped_early(11, 7)
        assert _map_shape(chapters, 301)["ok"] is False        # raw: rejected
        bounded, unmapped = _bounded_last_chapter(chapters, 301)
        assert (bounded[-1]["end_page"], unmapped) == (96, 204)
        assert unmapped / 301 > _MAX_UNMAPPED_TAIL_SHARE       # 67.8%
        assert _map_shape(bounded, 301)["unmapped_tail"] == 204
        assert "stopped early" in _map_shape(bounded, 301)["reason"]
        assert _chapters_plausible(chapters, 301) is False

    def test_the_span_rule_could_not_have_caught_it(self):
        # WHY the tail rule has to exist rather than a tighter span ratio: the
        # clip is 3x the median, and 3 < _MAX_SPAN_RATIO of 4.0, so above a
        # ~7-page median NO bounded map can ever trip the span rule again.
        for span in (7, 10, 20, 41, 60):
            bounded, _ = _bounded_last_chapter(self._stopped_early(5, span), 301)
            spans = [c["end_page"] - c["start_page"] + 1 for c in bounded]
            assert max(spans) <= _MAX_SPAN_RATIO * _median(spans), span

    def test_a_map_covering_only_the_first_quarter_is_rejected_at_every_span(self):
        # The failure is not specific to 7-page chapters. Units sized to cover
        # ~25% of a 301-page book and then stop, swept across the corpus's whole
        # range of legitimate chapter sizes. (60 is absent because three 60-page
        # units already reach page 180 of 301 — there is no truncation left to
        # catch, and the bound absorbs the rest.)
        for span in (5, 7, 10, 15, 20, 30, 41):
            chapters = self._stopped_early(max(3, round(75 / span)), span)
            _, unmapped = _bounded_last_chapter(chapters, 301)
            assert _chapters_plausible(chapters, 301) is False, (span, unmapped)

    def test_unbookmarked_back_matter_is_still_accepted(self):
        # THE case the bound exists for, and the one this must not break: 10
        # units of 20 pages in a 300-page book with 100 pages of unbookmarked
        # answers, glossary and index. The bound leaves 60 pages unmapped —
        # 20.0% of the book, five points under the gate — so the map is kept.
        chapters = _build_chapters_from_toc(_flat_toc(10, 20), 300)
        _, unmapped = _bounded_last_chapter(chapters, 300)
        assert unmapped == 60 and unmapped / 300 < _MAX_UNMAPPED_TAIL_SHARE
        assert _chapters_plausible(chapters, 300) is True

    def test_back_matter_up_to_a_third_of_the_book_is_still_accepted(self):
        # Headroom, pinned rather than assumed. The same 200-page body of 10
        # units still validates with a 120-page unbookmarked tail — 37.5% of the
        # book — because the last unit absorbs 60 of those pages and only the
        # remaining 80 (25.0% of 320) count as unmapped. That is the limit: one
        # page more of back matter and the map goes to the rescue path instead.
        for total in (300, 320):
            chapters = _build_chapters_from_toc(_flat_toc(10, 20), total)
            assert _chapters_plausible(chapters, total) is True, total
        assert _chapters_plausible(_build_chapters_from_toc(_flat_toc(10, 20), 321), 321) is False

    def test_a_rejected_map_reaches_the_llm_rescue_again(self, monkeypatch):
        # The point of rejecting it. Before the fix this book never got here:
        # the truncated map was plausible, so chapter_defs was never reset and
        # the `len(chapter_defs) <= 1` gate below never opened.
        from agent1_ingestion import vision_chapters

        rescued = [{"chapter_num": i, "title": f"Skill {i + 1}",
                    "start_page": i * 27, "end_page": i * 27 + 26} for i in range(11)]
        calls: list[str] = []

        def _fake_llm(extraction, client):
            calls.append("llm")
            return rescued

        monkeypatch.setattr(vision_chapters, "detect_chapters_from_text_llm", _fake_llm)

        toc = [TOCItem(level=1, title=f"Skill {i + 1} Compare and evaluate",
                       page_num=i * 7) for i in range(11)]
        book = structure_book(
            book_id="b", title="General Paper", author="CUP", isbn=None,
            extraction=_extraction(toc, 301), images=[], client=object(),
        )
        assert calls == ["llm"]
        assert book.chapters[-1].end_page == 300  # the whole book is mapped again

    def test_a_good_outline_never_reaches_the_llm_rescue(self):
        # The other side of the same gate: the legitimate back-matter book must
        # not start paying for a detection pass it does not need.
        from agent1_ingestion import vision_chapters

        def _boom(*_a, **_k):
            raise AssertionError("paid detection pass on a good outline")

        original = vision_chapters.detect_chapters_from_text_llm
        vision_chapters.detect_chapters_from_text_llm = _boom
        try:
            book = structure_book(
                book_id="b", title="Science", author="CUP", isbn=None,
                extraction=_extraction(_flat_toc(10, 20), 300), images=[],
                client=object(),
            )
        finally:
            vision_chapters.detect_chapters_from_text_llm = original
        assert book.total_chapters == 10


class TestFamilyRanking:
    def test_a_finer_family_beats_a_container(self):
        units = [{"chapter_num": i} for i in range(21)]
        parts = [{"chapter_num": i} for i in range(7)]
        assert _better_family(units, parts, "unit", "part") is True

    def test_three_stray_chapters_do_not_beat_twenty_real_parts(self):
        # A workbook that genuinely numbers its units "Part 1 … Part 20". Three
        # stray "Chapter N" strings anywhere in the text used to win outright,
        # because promotion over a container was unconditional.
        strays = [{"chapter_num": i} for i in range(3)]
        parts = [{"chapter_num": i} for i in range(20)]
        assert _better_family(strays, parts, "chapter", "part") is False

    def test_a_container_never_displaces_a_finer_family(self):
        parts = [{"chapter_num": i} for i in range(20)]
        units = [{"chapter_num": i} for i in range(9)]
        assert _better_family(parts, units, "part", "unit") is False


class TestRanges:
    def test_a_backwards_range_is_invalid(self):
        # Production book 994b8238 stored start_page=115, end_page=1.
        bad = [{"chapter_num": 0, "title": "A", "start_page": 0, "end_page": 10},
               {"chapter_num": 1, "title": "B", "start_page": 115, "end_page": 1}]
        assert _ranges_valid(bad, 128) is False
        assert _ranges_valid(_repair_chapter_ranges(bad, 128), 128) is True

    def test_repair_is_a_no_op_on_a_sound_map(self):
        good = [{"chapter_num": i, "title": f"Unit {i}", "start_page": i * 20,
                 "end_page": i * 20 + 19} for i in range(5)]
        assert _repair_chapter_ranges([dict(c) for c in good], 100) == good

    def test_out_of_range_and_duplicate_starts_are_dropped(self):
        messy = [{"chapter_num": 0, "title": "A", "start_page": 0, "end_page": 9},
                 {"chapter_num": 1, "title": "B", "start_page": 0, "end_page": 9},
                 {"chapter_num": 2, "title": "C", "start_page": 999, "end_page": 999}]
        fixed = _repair_chapter_ranges(messy, 100)
        assert [c["start_page"] for c in fixed] == [0]
        assert [c["chapter_num"] for c in fixed] == [0]


# ── labels ───────────────────────────────────────────────────────────────────

class TestLabels:
    def test_unicode_roman_numerals_are_recognised(self):
        # Verified false before this change: the numeral is U+2160, and because
        # it is a letter-number neither `\s+` nor `\b` could find the boundary.
        m = _LABEL_RE.match("PartⅠ Perspective and Process")
        assert m is not None
        assert _marker_number(m.group(2)) == 1
        assert m.group(3) == "Perspective and Process"

    def test_ascii_roman_numerals_are_recognised(self):
        assert _marker_number(_LABEL_RE.match("Part IV Tools").group(2)) == 4
        assert _marker_number(_LABEL_RE.match("Section II Overview").group(2)) == 2

    def test_bracketed_numbers_are_recognised(self):
        # "Unit (1):" is the standard heading convention in Egyptian and wider
        # Arab-world textbooks. Verified false before this change, on a real
        # organic upload: a 156-page Grade 5 science book indexed as ONE 76-page
        # unit because every "Unit (n)" after the first was invisible here,
        # leaving 78 pages — half the book — unmapped and untaught.
        m = _LABEL_RE.match("Unit (1): Interaction of Living Organisms - Plants")
        assert m is not None
        assert _marker_number(m.group(2)) == 1
        assert m.group(3) == "Interaction of Living Organisms - Plants"

        for s, n in (("Unit (2): Matter and Energy", 2),
                     ("Chapter (3): Photosynthesis", 3),
                     ("Lesson (2): Roots", 2),
                     ("Section [2] Forces", 2),
                     ("Unit ( 4 ) : Water", 4),
                     ("Chapter (10): Ecosystems", 10)):
            hit = _LABEL_RE.match(s)
            assert hit is not None, s
            assert _marker_number(hit.group(2)) == n, s

    def test_words_that_merely_start_with_a_label_do_not_match(self):
        # The (?!\w) guard must survive the bracket change — the closing bracket
        # is matched AFTER it for exactly this reason. "Unitary" and "Unit (a)"
        # are the cases the optional opening bracket could plausibly have broken.
        for s in ("Parties and Politics", "Partition Theory", "Themes of Biology",
                  "Weekend revision", "Partly cloudy", "Sections and topics",
                  "Modules", "Unitary method", "Unit (a) overview"):
            assert _LABEL_RE.match(s) is None, s

    def test_digits_and_number_words_still_work(self):
        assert _marker_number(_LABEL_RE.match("Chapter 12").group(2)) == 12
        assert _marker_number(_LABEL_RE.match("Lesson Three").group(2)) == 3
        assert _LABEL_RE.match("Unit 3: Selecting hardware").group(3) == "Selecting hardware"


class TestFileBookmarks:
    def test_extension_less_filename_bookmarks_are_rejected(self):
        # Production book 994b8238: all five "chapters" were export slugs, and
        # the old test needed a literal ".pdf" to notice.
        toc = [TOCItem(level=1, title=f"esl_cie_asl_genpaper_1ed_tr_ch1.{i}_wksht_TOR",
                       page_num=i * 20) for i in range(5)]
        assert _toc_is_usable(toc, 128) is False

    def test_a_real_outline_is_still_usable(self):
        assert _toc_is_usable(_flat_toc(8, 30), 240) is True


# ── end to end through structure_book ────────────────────────────────────────

class TestStructureBook:
    def test_nested_book_structures_at_the_chapter_level(self):
        book = structure_book(
            book_id="b", title="Business Dynamics", author="Sterman", isbn=None,
            extraction=_extraction(_nested_toc(), 1008), images=[],
        )
        assert book.total_chapters == 21
        assert max(c.end_page - c.start_page + 1 for c in book.chapters) < 120

    def test_flat_book_is_unchanged(self):
        book = structure_book(
            book_id="b", title="Science Y7", author="CUP", isbn=None,
            extraction=_extraction(_flat_toc(9, 38), 344), images=[],
        )
        assert book.total_chapters == 9
        assert book.chapters[0].start_page == 0
        assert book.chapters[-1].end_page == 343

    def test_the_last_chapter_is_not_stretched_across_the_book(self):
        # Detection stops at page 71 on a 301-page book. The tail must not be
        # bolted onto a 7-page chapter.
        items = _items(301)
        detected = [DocItem(item_type="section_header", text=f"Chapter {i + 1}",
                            page_num=i * 7, level=1) for i in range(10)]
        book = structure_book(
            book_id="b", title="General Paper", author="CUP", isbn=None,
            extraction=_extraction([], 301, items=detected + items), images=[],
        )
        assert book.chapters[-1].end_page - book.chapters[-1].start_page + 1 <= 30
