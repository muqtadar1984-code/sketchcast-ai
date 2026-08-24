"""Chapter ALTITUDE — is a detected unit a chapter, or the Part that contains
the chapters?

The trigger: a 1,008-page university textbook whose outline nested ~21
chapters under 7 Parts and indexed as 8 "chapters" of 114-254 pages, each
offering 19-28 lesson parts; 17 kits were generated against that map. Health
called it "excellent", because the only structure signal was
``n_chapters >= 3``.

(2026-08-24: the bookmark rung these checks were first built against is GONE
— founder decision, see the structurer's block comment — so every map here is
built directly or by the TEXT detectors. The shape/coverage/altitude rules
are rung-independent and carry over unchanged; the end-to-end text-rung
regressions live in tests/test_text_rungs.py.)
"""

from __future__ import annotations

from types import SimpleNamespace

from agent1_ingestion.extractor import DocItem
from agent1_ingestion.structurer import (_COVERAGE_MARGIN, _LABEL_RE,
                                         _MAX_SPAN_RATIO,
                                         _MAX_UNMAPPED_TAIL_SHARE,
                                         _better_family, _bounded_last_chapter,
                                         _chapters_plausible,
                                         _detect_labeled_chapters, _label_run,
                                         _map_coverage, _map_shape, _marker_key,
                                         _MIN_ESCALATION_COVERAGE,
                                         _SHAPE_MIN_PAGES,
                                         _marker_number, _median,
                                         _page_text_stats, _pick_detected_map,
                                         _ranges_valid,
                                         _repair_chapter_ranges,
                                         structure_book)

# Roughly one academic page of text — the shape checks estimate lesson-parts
# from the extracted volume, so page ranges alone are not enough.
_PAGE_TEXT = (
    "Systems thinking asks us to look past the events we notice to the structure "
    "that keeps producing them. In this section we build a stock and flow model of "
    "a simple supply chain, run it, and compare what it does with what the managers "
    "in the case expected it to do. The gap between the two is the lesson. "
) * 9

# Distinct real-looking unit titles, so synthetic maps never trip the
# duplicate-title or family signals a REAL flat book would not trip either.
_TITLES = ["Cells and organisms", "Materials and matter", "Forces and motion",
           "Energy transfers", "Earth and space", "Ecosystems", "Light and sound",
           "Chemical reactions", "Magnetism and electricity", "Waves",
           "Heat and temperature", "The human body"]


def _items(total_pages: int) -> list[DocItem]:
    return [DocItem(item_type="paragraph", text=_PAGE_TEXT, page_num=p, level=0)
            for p in range(total_pages)]


def _extraction(toc, total_pages, items=None):
    return SimpleNamespace(
        toc=toc, total_pages=total_pages,
        items=items if items is not None else _items(total_pages),
        readability_score=0.9, extraction_backend="pymupdf", markdown="",
    )


def _flat_map(n: int, pages_each: int, total_pages: int) -> list[dict]:
    """n units of ``pages_each`` pages, the last elastic to the end of the book
    — the exact shape every detector emits (each end from the next start, the
    final unit running to total_pages-1)."""
    return [{"chapter_num": i,
             "title": f"Unit {i + 1} {_TITLES[i % len(_TITLES)]}",
             "start_page": i * pages_each,
             "end_page": (i + 1) * pages_each - 1 if i < n - 1 else total_pages - 1}
            for i in range(n)]


def _heading_items(starts_titles, total_pages: int) -> list[DocItem]:
    """Level-1 unit headings at the given (page, title) points, over body text
    — the born-digital shape the text rungs read now that bookmarks are gone."""
    out = [DocItem(item_type="title", text=t, page_num=p, level=1)
           for p, t in starts_titles]
    out += _items(total_pages)
    out.sort(key=lambda i: (i.page_num, -i.level))
    return out


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
    def test_unmarked_back_matter_does_not_condemn_a_good_map(self):
        # 10 units of 20 pages in a 300-page book: 100 pages of answers,
        # glossary and index carry no detectable heading, so the final unit
        # measures 40% of the book and 6x its siblings. Cambridge/NCERT-style
        # textbooks carry exactly this. Judged UNBOUNDED the map was discarded;
        # judged as it will actually be stored — with the last-chapter bound
        # applied — it is a perfectly good 10-chapter map.
        chapters = _flat_map(10, 20, 300)
        assert _map_shape(chapters, 300)["ok"] is False       # raw: rejected
        assert _chapters_plausible(chapters, 300) is True     # bounded: kept

    def test_the_bound_reports_the_pages_it_leaves_unmapped(self):
        # Silent content loss is worse than the over-long chapter it replaces:
        # unmapped pages are never extracted, analysed or taught.
        chapters = _flat_map(10, 20, 300)
        bounded, unmapped = _bounded_last_chapter(chapters, 300)
        assert unmapped > 0
        assert bounded[-1]["end_page"] + 1 + unmapped == 300
        # …and the input is not mutated, so a plausibility probe cannot commit.
        assert chapters[-1]["end_page"] == 299

    def test_a_normal_tail_is_absorbed_with_nothing_dropped(self):
        chapters = _flat_map(9, 38, 344)
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
        """``n`` units of ``span`` pages, then nothing: the classic shape of
        detection giving up. The final unit is elastic to the end of the book,
        exactly as every detector builds it."""
        return _flat_map(n, span, total_pages)

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

    def test_unmarked_back_matter_is_still_accepted(self):
        # THE case the bound exists for, and the one this must not break: 10
        # units of 20 pages in a 300-page book with 100 pages of unheaded
        # answers, glossary and index. The bound leaves 60 pages unmapped —
        # 20.0% of the book, five points under the gate — so the map is kept.
        chapters = _flat_map(10, 20, 300)
        _, unmapped = _bounded_last_chapter(chapters, 300)
        assert unmapped == 60 and unmapped / 300 < _MAX_UNMAPPED_TAIL_SHARE
        assert _chapters_plausible(chapters, 300) is True

    def test_back_matter_up_to_a_third_of_the_book_is_still_accepted(self):
        # Headroom, pinned rather than assumed. The same 200-page body of 10
        # units still validates with a 120-page unheaded tail — 37.5% of the
        # book — because the last unit absorbs 60 of those pages and only the
        # remaining 80 (25.0% of 320) count as unmapped. That is the limit: one
        # page more of back matter and the map goes to the rescue path instead.
        for total in (300, 320):
            assert _chapters_plausible(_flat_map(10, 20, total), total) is True, total
        assert _chapters_plausible(_flat_map(10, 20, 321), 321) is False

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

        # Printed "Chapter N …" headings stopping at page 70 of 301 — the
        # truncated map now arrives via the labelled text rung.
        items = _heading_items(
            [(i * 7, f"Chapter {i + 1} Compare and evaluate") for i in range(11)], 301)
        book = structure_book(
            book_id="b", title="General Paper", author="CUP", isbn=None,
            extraction=_extraction([], 301, items=items), images=[], client=object(),
        )
        assert calls == ["llm"]
        assert book.chapters[-1].end_page == 300  # the whole book is mapped again

    def test_a_good_map_never_reaches_the_llm_rescue(self):
        # The other side of the same gate: the legitimate back-matter book must
        # not start paying for a detection pass it does not need.
        from agent1_ingestion import vision_chapters

        def _boom(*_a, **_k):
            raise AssertionError("paid detection pass on a good book")

        original = vision_chapters.detect_chapters_from_text_llm
        vision_chapters.detect_chapters_from_text_llm = _boom
        items = _heading_items(
            [(i * 20, f"Unit {i + 1} {_TITLES[i]}") for i in range(10)], 300)
        try:
            book = structure_book(
                book_id="b", title="Science", author="CUP", isbn=None,
                extraction=_extraction([], 300, items=items), images=[],
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


class TestConceptFamily:
    """CONCEPT is the teaching unit in the Egyptian curriculum: the book that
    exposed this nests Theme (2) → Unit (2) → Concept (6) → Lesson (~27) over 156
    pages, and the six ~26-page Concepts are what a teacher assigns. With the word
    missing from _LABEL_RE all six were invisible, both container families were
    two entries long (under the 3-entry bar), and the book fell through to heading
    inference — 2 chapters, 132 of 156 pages unmapped."""

    def test_concept_headings_are_recognised(self):
        for s, key, title in (
            ("Concept 1.1 Plants Needs", (1, 1), "Plants Needs"),
            ("Concept (1.2) Energy Flow In Ecosystem", (1, 2), "Energy Flow In Ecosystem"),
            ("CONCEPT 2.3: Comparing changes in matter", (2, 3), "Comparing changes in matter"),
            ("Concept 1 : Plants Needs", (1, 0), "Plants Needs"),
            ("Concept Two Energy", (2, 0), "Energy"),
        ):
            m = _LABEL_RE.match(s)
            assert m is not None, s
            assert _marker_key(m.group(2)) == key, s
            assert m.group(3) == title, s

    def test_the_ordinary_noun_cannot_reach_the_number_group(self):
        # Every word added to _LABEL_RE is a new false-positive surface, and
        # "concept" earns its place only because its ordinary-noun forms cannot
        # reach the number: "Concepts…" and "Conceptual…" put a letter where the
        # marker has to be, and the (?!\w) guard rejects a bare roman numeral
        # glued to a longer word ("Conception" is not "Concept" + "i").
        for s in ("Concepts of Physics", "Conceptual framework", "Concept map",
                  "Conceptions of matter", "Concepts and skills"):
            assert _LABEL_RE.match(s) is None, s


class TestDecimalMarkers:
    """Decimal families are ordered by the (major, minor) pair, because there is
    no consecutive integer sequence in "1.1, 1.2, 1.3, 2.1, 2.2, 2.3" for the
    integer rule to find."""

    def test_the_key_separates_whole_numbers_from_decimals(self):
        assert _marker_key("7") == (7, 0)
        assert _marker_key("2.3") == (2, 3)
        assert _marker_key("iv") == (4, 0)
        assert _marker_key("three") == (3, 0)
        # A decimal marker is NOT an integer marker: _marker_number still refuses
        # it, which is what keeps the running-header detector's integer cluster
        # keys clean.
        assert _marker_number("2.3") is None

    def test_a_decimal_run_is_built_in_printed_order(self):
        markers = {(2, 3): 0, (1, 1): 0, (2, 1): 0, (1, 3): 0, (1, 2): 0, (2, 2): 0}
        assert _label_run(markers, decimal=True) == [
            (1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3)]

    def test_a_decimal_family_still_has_to_be_dense(self):
        # The density requirement is the integer rule applied on BOTH axes, not a
        # weaker one — majors consecutive from 1, minors consecutive from 1 —
        # so a gap cuts the run exactly where a missing "Chapter 4" would.
        assert _label_run({(1, 1): 0, (1, 2): 0, (1, 4): 0}, decimal=True) == [(1, 1), (1, 2)]
        assert _label_run({(1, 1): 0, (1, 2): 0, (3, 1): 0}, decimal=True) == [(1, 1), (1, 2)]
        # A sub-numbering that never starts at 1 yields nothing, exactly as a lone
        # "Chapter 7" does.
        assert _label_run({(3, 1): 0, (3, 2): 0, (3, 3): 0}, decimal=True) == []

    def test_the_integer_rule_is_untouched(self):
        # THE rule that must not be weakened: three stray "Chapter N" mentions
        # cannot form a map just because they sort.
        assert _label_run({(1, 0): 0, (3, 0): 0, (7, 0): 0}, decimal=False) == [(1, 0)]
        assert _label_run({(1, 0): 0, (2, 0): 0, (3, 0): 0}, decimal=False) == [
            (1, 0), (2, 0), (3, 0)]
        assert _label_run({(2, 0): 0, (3, 0): 0}, decimal=False) == []

    def test_a_sub_numbered_family_does_not_beat_the_family_it_subdivides(self):
        # "Lesson 1.1 … 1.5" printed inside chapter 1 of a 200-page book is a
        # dense, strictly-increasing, perfectly valid run of 5, and on LENGTH
        # alone it beats the 10 real "Chapter N" headings. It reaches 21 pages
        # against their 181, so the reach rule refuses it.
        subs = [{"chapter_num": i, "start_page": i * 5} for i in range(5)]
        chapters = [{"chapter_num": i, "start_page": i * 20} for i in range(10)]
        assert _better_family(subs, chapters, "lesson", "chapter") is False
        assert _better_family(chapters, subs, "chapter", "lesson") is True

    def test_a_decimal_running_header_still_clusters_on_its_unit(self):
        # REGRESSION GUARD for the change itself. A book that prints
        # "Unit 3.1 Feedback loops" on every page of unit 3 used to cluster on the
        # "3" — the old pattern simply stopped at the integer. Teaching the number
        # group about decimals would have made _marker_number reject the whole
        # marker and taken this book's running headers away, so the running-header
        # detector reads the MAJOR. Same clusters, cleaner titles.
        from agent1_ingestion.structurer import _detect_running_header_chapters
        items = []
        for unit in range(1, 6):
            for k in range(8):
                pg = (unit - 1) * 8 + k
                items.append(DocItem(item_type="paragraph", level=0, page_num=pg,
                                     text=f"Unit {unit}.{k % 3 + 1} Feedback loops"))
        chapters = _detect_running_header_chapters(items, 40)
        assert [c["start_page"] for c in chapters] == [0, 8, 16, 24, 32]
        assert chapters[0]["title"] == "Feedback loops"

    def test_a_decimal_layer_is_not_confused_with_a_bare_subsection_layer(self):
        # _SUBSEC_RE identifies a BARE decimal ("3.2 Feedback loops") — the
        # contents-page and title-boundary helpers' shape. A LABELLED decimal
        # ("Concept 1.1 …") never collides with it — pinned in both
        # directions. (_depth_is_sections, the outline-level twin, died with
        # the bookmark rung, 2026-08-24; the bare-decimal layer is now kept
        # out of the chapter list by the labelled detectors simply never
        # matching it, and by the shape checks when a detector-free map
        # carries it.)
        from agent1_ingestion.structurer import _SUBSEC_RE
        assert _SUBSEC_RE.match("Concept 1.1 Plants Needs") is None
        assert _SUBSEC_RE.match("3.2 Feedback loops") is not None
        assert _LABEL_RE.match("3.2 Feedback loops") is None


class TestCoveragePreference:
    """PREFER THE MAP THAT COVERS MORE OF THE BOOK. The rule that catches a
    wrong-altitude regression without anyone having to notice the altitude: on the
    Egyptian book one detector produced 24 mapped pages of 156 and another 154,
    and nothing compared them — the cascade took whichever rung fired first."""

    def _flat(self, n, span, first=0):
        return [{"chapter_num": i, "title": f"Unit {i + 1}", "start_page": first + i * span,
                 "end_page": first + i * span + span - 1} for i in range(n)]

    def test_coverage_is_measured_on_the_map_as_it_will_be_stored(self):
        # Bounded, like _chapters_plausible: an unbounded map overstates its own
        # coverage by exactly the tail the bound is about to discard.
        stretched = self._flat(10, 20)
        stretched[-1]["end_page"] = 299
        assert _map_coverage(stretched, 300) == 240 / 300
        # …and a map that misses the FRONT of the book scores as badly as one
        # that stops early, which the unmapped-tail rule cannot see at all.
        assert _map_coverage(self._flat(5, 20, first=100), 200) == 100 / 200

    def test_the_more_complete_map_wins(self):
        # The Egyptian book in miniature: heading inference found a 2-page
        # contents entry and one unit; the Concept family found all six.
        thin = [{"chapter_num": 0, "title": "Table of Contents", "start_page": 0, "end_page": 1},
                {"chapter_num": 1, "title": "System", "start_page": 2, "end_page": 155}]
        concepts = [{"chapter_num": i, "title": f"Concept {i}", "start_page": 2 + i * 26,
                     "end_page": 2 + i * 26 + 25} for i in range(6)]
        assert _map_coverage(thin, 156) < 0.2 and _map_coverage(concepts, 156) > 0.9
        # …whichever order the cascade offers them in.
        assert _pick_detected_map([concepts, thin], 156)[0]["title"] == "Concept 0"
        assert _pick_detected_map([thin, concepts], 156)[0]["title"] == "Concept 0"

    def test_coverage_never_overrides_the_altitude_rules(self):
        # 8 Parts of 126 pages cover 100% of a 1,008-page book and are exactly the
        # wrong-altitude map this module exists to reject; 18 chapters of 42 pages
        # cover 83% and are right. The coverage gap is 17 points — well past the
        # margin — so this only comes out right because PLAUSIBILITY is compared
        # first and absolutely.
        parts = self._flat(8, 126)
        chapters = self._flat(18, 42)
        assert _chapters_plausible(parts, 1008) is False
        assert _chapters_plausible(chapters, 1008) is True
        assert _map_coverage(parts, 1008) - _map_coverage(chapters, 1008) > _COVERAGE_MARGIN
        assert len(_pick_detected_map([parts, chapters], 1008)) == 18
        assert len(_pick_detected_map([chapters, parts], 1008)) == 18

    def test_one_giant_chapter_does_not_win_on_coverage(self):
        # The tension the margin alone cannot resolve: a single whole-book chapter
        # covers 100% of every book there is. Both maps here are plausible (a
        # 1-chapter map is below the shape checks' size guard), and the coverage
        # gap clears the margin — it is the altitude bar that refuses it.
        chapters = self._flat(12, 20)
        whole = [{"chapter_num": 0, "title": "Full Document", "start_page": 0, "end_page": 319}]
        assert _chapters_plausible(whole, 320) is True
        assert _map_coverage(whole, 320) - _map_coverage(chapters, 320) > _COVERAGE_MARGIN
        assert len(_pick_detected_map([chapters, whole], 320)) == 12

    def test_an_equally_complete_map_leaves_the_cascade_order_alone(self):
        # Coverage breaks ties and rejects clearly-worse maps; it does not
        # reshuffle equals. The higher-signal detector keeps its map.
        first = self._flat(10, 20)
        second = [dict(c, title=f"Runner-up {c['chapter_num']}") for c in first]
        assert _pick_detected_map([first, second], 300)[0]["title"] == "Unit 1"


class TestHerBook:
    """END TO END on the Egyptian Grade 5 science book's heading sequence.

    Theme (1) System / Unit (1) Interaction of living organisms
        Concept 1.1 Plants Needs / 1.2 Energy Flow In Ecosystem / 1.3 Changes …
    Theme (2) Matter and Energy / Unit (2) Particles in motion
        Concept 2.1 Matter in the world … / 2.2 Describing … / 2.3 Comparing …

    Measured on the live map before this change: two chapters — a 2-page contents
    entry and one 22-page unit — with 132 of 156 pages unmapped and untaught.
    """

    _CONCEPTS = [(2, "Concept (1.1) Plants Needs", 5),
                 (28, "Concept (1.2) Energy Flow In Ecosystem", 4),
                 (54, "Concept (1.3) Changes in the food web", 4),
                 (80, "Concept (2.1) Matter in the world around us", 5),
                 (106, "Concept (2.2) Describing and measuring matter", 4),
                 (132, "Concept (2.3) Comparing changes in matter", 5)]

    def _items(self) -> list[DocItem]:
        # The extractor tagged the contents page and the first theme's banner word
        # as level-1 titles and everything below as headings — the shape that
        # reproduces the production map exactly.
        out = [DocItem(item_type="title", text="Table of Contents", page_num=0, level=1),
               DocItem(item_type="title", text="System", page_num=2, level=1)]
        for pg, theme, unit in ((2, "Theme (1) System", "Unit (1) Interaction of living organisms"),
                                (80, "Theme (2) Matter and Energy", "Unit (2) Particles in motion")):
            out.append(DocItem(item_type="section_header", text=theme, page_num=pg, level=2))
            out.append(DocItem(item_type="section_header", text=unit, page_num=pg, level=2))
        for pg, title, lessons in self._CONCEPTS:
            out.append(DocItem(item_type="section_header", text=title, page_num=pg, level=2))
            for i in range(lessons):  # too fine to be the teaching unit
                out.append(DocItem(item_type="section_header", text=f"Lesson ({i + 1}) Investigating",
                                   page_num=pg + 2 + i * 4, level=3))
        out += _items(156)
        out.sort(key=lambda i: (i.page_num, i.level))
        return out

    def test_the_concepts_are_the_detected_chapters(self):
        chapters = _detect_labeled_chapters(self._items(), 156)
        assert [c["title"] for c in chapters] == [
            "Plants Needs", "Energy Flow In Ecosystem", "Changes in the food web",
            "Matter in the world around us", "Describing and measuring matter",
            "Comparing changes in matter"]
        assert [c["start_page"] for c in chapters] == [2, 28, 54, 80, 106, 132]

    def test_the_book_indexes_as_six_concepts_with_nothing_lost(self):
        book = structure_book(
            book_id="b", title="Science Grade 5", author="MOE Egypt", isbn=None,
            extraction=_extraction([], 156, items=self._items()), images=[],
        )
        assert book.total_chapters == 6
        covered = sum(c.end_page - c.start_page + 1 for c in book.chapters)
        assert covered == 154 and book.chapters[-1].end_page == 155  # was 24 of 156
        # …at the right altitude: not the 2 Themes (78pp each, containers) and not
        # the 27 Lessons.
        assert 20 <= _median([c.end_page - c.start_page + 1 for c in book.chapters]) <= 30


# (TestFileBookmarks is gone with the bookmark rung, 2026-08-24. The filename
# CLASS lives on in chapter_quality._looks_like_filename — judged on OUTCOMES,
# pinned with the fleet fixtures in tests/test_chapter_quality.py.)


# ── end to end through structure_book ────────────────────────────────────────

class TestStructureBook:
    def test_nested_book_structures_at_the_chapter_level(self):
        # Sterman's shape off its PRINTED headings: 7 Part banners containing
        # 21 Chapter headings. The chapter family must win the altitude fight
        # exactly as it did when the same shape arrived by outline.
        starts_titles = [(i * 144, f"Part {'ⅠⅡⅢⅣⅤⅥⅦ'[i]} Perspective") for i in range(7)]
        starts_titles += [(i * 48, f"Chapter {i + 1} {_TITLES[i % len(_TITLES)]}")
                          for i in range(21)]
        book = structure_book(
            book_id="b", title="Business Dynamics", author="Sterman", isbn=None,
            extraction=_extraction([], 1008, items=_heading_items(starts_titles, 1008)),
            images=[],
        )
        assert book.total_chapters == 21
        assert max(c.end_page - c.start_page + 1 for c in book.chapters) < 120

    def test_flat_book_is_unchanged(self):
        items = _heading_items(
            [(i * 38, f"Unit {i + 1} {_TITLES[i]}") for i in range(9)], 344)
        book = structure_book(
            book_id="b", title="Science Y7", author="CUP", isbn=None,
            extraction=_extraction([], 344, items=items), images=[],
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


class TestPoorCoverageEscalates:
    """A map can be non-empty and still be worthless.

    From a live book: a 156-page Grade 5 science text produced TWO chapters — a
    2-page contents entry and one 76-page unit — and every gate let it through.
    The Claude rung was fenced behind ``len(chapter_defs) <= 1`` so it never ran,
    and ``_map_shape`` returns ok early below ``_SHAPE_MIN_CHAPTERS`` so a
    2-chapter map is never shape-checked at all. The book's real units are six
    "Concept 1.1 … 2.3" markers that sit INLINE IN BODY TEXT rather than as
    headings, so no heading-based detector can reach them — reading prose is
    exactly what the text-LLM rung is for, and it was the one thing forbidden.
    """

    @staticmethod
    def _map(spans):
        chapters, page = [], 0
        for span in spans:
            chapters.append({"chapter_num": len(chapters), "title": f"C{len(chapters)}",
                             "start_page": page, "end_page": page + span - 1})
            page += span
        return chapters

    @staticmethod
    def _escalates(chapters, total_pages):
        # Mirrors the condition in structure_book.
        return (len(chapters) > 1
                and total_pages >= _SHAPE_MIN_PAGES
                and _map_coverage(chapters, total_pages) < _MIN_ESCALATION_COVERAGE)

    def test_her_book_shape_escalates(self):
        assert self._escalates(self._map([2, 76]), 156) is True

    def test_detection_stopping_a_quarter_in_escalates(self):
        assert self._escalates(self._map([7] * 11), 301) is True

    def test_legitimate_shapes_never_pay_for_a_claude_call(self):
        # These are the maps pinned elsewhere in this file as CORRECT. If any of
        # them starts escalating, the threshold is billing good books.
        for spans, total in ((([20] * 10), 300),      # + 100pp unbookmarked back matter
                             (([30] * 8), 240),
                             (([60] * 5), 300),
                             (([26] * 6), 156)):      # the map her book should produce
            assert self._escalates(self._map(spans), total) is False, (spans, total)

    def test_a_short_upload_is_never_escalated(self):
        # Under _SHAPE_MIN_PAGES one chapter legitimately IS the document.
        assert self._escalates(self._map([20, 20]), 40) is False


class TestNoRuntPartBillsACredit:
    """A hard-split remainder must not become a part of its own.

    build_chapter_parts cuts a monolithic OCR string on a fixed
    MAX_ANALYSIS_CHARS stride, so unless a chapter's text lands near a multiple
    of that stride the final chunk is whatever was left over. Measured on
    realistic prose before the fix: tails of 3, 20, 37, 44, 87 and 172 words —
    at EVERY size tried, not as a rare edge.

    That runt was a full part: its own analysis call, script, slides, ~3-minute
    video (the duration floor keeps it there), upload — and its own CREDIT,
    because credit_ledger_sync resets a presentation's units to the number of
    videos actually rendered. A teacher paid a credit for a video built from
    twenty words.

    Reading chapters to their own end (2026-08-24) made this MORE likely by
    making multi-chunk chapters the norm, which is why it is fixed here.
    """

    SAMPLE = ("the cell wall controls what enters and leaves a plant cell while the "
              "membrane stays flexible and thin under most conditions ")

    def _parts(self, chars):
        from agent2_analysis.analyzer import build_chapter_parts

        text = (self.SAMPLE * (chars // len(self.SAMPLE) + 2))[:chars]
        return build_chapter_parts({"sections": [{
            "section_title": "Content", "section_type": "body",
            "content": text, "page_num": 0, "subsections": []}]}), text

    def test_no_generated_part_is_a_runt(self):
        from agent2_analysis.analyzer import _MIN_TAIL_WORDS

        for chars in (45241, 45000, 46000, 60500, 75200, 90100, 30001, 15001):
            parts, _ = self._parts(chars)
            if len(parts) == 1:
                continue
            assert parts[-1]["words"] >= _MIN_TAIL_WORDS, (
                f"{chars} chars -> runt tail of {parts[-1]['words']} words "
                f"= a junk video and a billed credit")

    def test_merging_never_drops_a_single_word(self):
        """The whole point: this is a MERGE, not a trim. It runs alongside work
        whose purpose is that nothing is lost, so it must lose nothing."""
        for chars in (45241, 60500, 90100):
            parts, text = self._parts(chars)
            joined = " ".join(p["text"] for p in parts)
            body = [w for w in joined.split() if not w.startswith("#")]
            assert len(body) >= len(text.split()), f"{chars}: words went missing"
            # the runt's own words survive inside the part that absorbed it
            assert text.split()[-1] in parts[-1]["text"]

    def test_a_legitimately_short_chapter_still_makes_one_part(self):
        parts, _ = self._parts(800)
        assert len(parts) == 1 and parts[0]["words"] > 0

    def test_the_merge_only_ever_lowers_the_credit_count(self):
        from agent2_analysis.analyzer import MAX_ANALYSIS_CHARS
        import math

        for chars in (45241, 60500, 75200, 90100):
            parts, _ = self._parts(chars)
            assert len(parts) <= math.ceil(chars / MAX_ANALYSIS_CHARS)

    def test_word_count_is_recomputed_after_the_merge(self):
        """`words` drives the video-duration plan; a stale count would plan the
        absorbed part's runtime from the wrong length."""
        parts, _ = self._parts(60500)
        for p in parts:
            assert p["words"] == len(p["text"].split())
