"""Chapter-list OUTCOME validation — script-agnostic junk detection.

The incident being encoded (2026-08-23, Sara Junaidi, book e0459f87): a KamiHQ
scan of "Science Learner's Book 8" carried 13 human-typed junk bookmarks —
'LEANERS BOOK 8 1' … '12', the misspelled cover title plus a serial — and
indexed to 13 chapters whose boundaries described nothing, at health 82 "good"
with problems []. Every existing guard was a point filter or a page-geometry
check; nothing anywhere judged whether the winning TITLES were chapter titles.

These tests pin the validator's two duties:
  * flag the incident's shapes (raw junk bookmarks AND the cosmetically-healed
    stored list) in ANY script — Latin, Arabic, Devanagari, CJK, RTL titles,
    Arabic-Indic digits, U+2160 roman numerals;
  * stay silent on every healthy shape the corpus contains, generic
    "Chapter N" labels in any language included.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent1_ingestion.book_health import compute_book_health
from agent1_ingestion.chapter_quality import (assess_chapter_map,
                                              covered_page_count,
                                              uncovered_pages)

FIXTURE = Path(__file__).parent / "fixtures" / "sara_science_learners_book_8.json"


def _defs(rows):
    return [
        {"chapter_num": i, "title": t, "start_page": s, "end_page": e}
        for i, (t, s, e) in enumerate(rows)
    ]


def _flat(titles, span=20):
    return _defs([(t, i * span, i * span + span - 1) for i, t in enumerate(titles)])


def _sara_raw():
    """The 13 junk bookmarks exactly as the real PDF carries them."""
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    starts = [t["page_1based"] - 1 for t in fx["toc"]]
    rows = []
    for i, t in enumerate(fx["toc"]):
        end = (starts[i + 1] - 1) if i + 1 < len(starts) else fx["total_pages"] - 1
        rows.append((t["title"], starts[i], end))
    return _defs(rows), fx["total_pages"]


def _sara_stored():
    """The incident's STORED list — titles cosmetically healed onto the junk
    boundaries, chapter 3 relocated to 22-25 leaving the 36-page hole."""
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bounds = [(0, 5), (6, 7), (8, 21), (22, 25), (62, 127), (128, 165),
              (166, 177), (178, 193), (194, 197), (198, 221), (222, 240),
              (241, 325), (326, 340)]
    return _defs(list(zip(fx["stored_incident_titles"], *zip(*bounds)))), fx["total_pages"]


def _sara_geometry(titles):
    """The incident's exact 13 page boundaries glued onto any 13 titles —
    the geometry a scanner splitting at arbitrary points produces (a 2-page
    'chapter' beside an 85-page one), which is the CORROBORATION the family
    signal needs before it may condemn."""
    assert len(titles) == 13
    bounds = [(0, 5), (6, 7), (8, 21), (22, 61), (62, 127), (128, 165),
              (166, 177), (178, 193), (194, 197), (198, 221), (222, 240),
              (241, 325), (326, 340)]
    return _defs([(t, s, e) for t, (s, e) in zip(titles, bounds)]), 341


# ── the incident's own shapes ────────────────────────────────────────────────


class TestSaraShapes:
    def test_the_raw_bookmarks_are_suspect_family_plus_span_variance(self):
        chapters, total = _sara_raw()
        v = assess_chapter_map(chapters, total)
        assert v["suspect"] is True
        # 12 of 13 share the 'leaners book 8' stem…
        assert v["signals"]["family_members"] == 12
        assert v["signals"]["duplicate_family"] > 0.9
        # …CORROBORATED by the scanner's arbitrary-split geometry (2 pages
        # beside 85). The family never condemns alone: family-alone is what a
        # legitimate serial labelling scheme looks like too.
        assert v["signals"]["span_variance"] is True
        assert any("near-copies" in r for r in v["reasons"])

    def test_the_stored_healed_list_is_still_suspect(self):
        # The heal glued real section headings ('2.4 Paper chromatography')
        # onto the junk boundaries, so the family signal is gone — the
        # decimal-skip and span-variance signals must carry it instead.
        chapters, total = _sara_stored()
        v = assess_chapter_map(chapters, total)
        assert v["suspect"] is True
        assert v["signals"]["decimal_nonseq"] is True
        assert v["signals"]["span_variance"] is True

    def test_the_36_page_hole_is_visible(self):
        chapters, total = _sara_stored()
        gaps = uncovered_pages(chapters, total)
        assert gaps["holes"] == 36  # pages 26-61, mid-book — tail-only saw 0
        assert gaps["tail"] == 0


# ── healthy shapes must stay silent (requirement 6) ──────────────────────────


class TestHealthyShapes:
    def test_generic_chapter_labels_are_not_a_family(self):
        # 'Chapter 1'..'Chapter 12' all stem to the single word 'chapter' —
        # how the detectors label a book whose printed titles weren't found.
        # A single-word stem is a generic label in every language, never junk.
        v = assess_chapter_map(_flat([f"Chapter {i + 1}" for i in range(12)]), 240)
        assert v["suspect"] is False

    def test_real_titled_chapters_pass(self):
        v = assess_chapter_map(
            _flat(["Unit 1 Living things", "Unit 2 Materials", "Unit 3 Forces",
                   "Unit 4 Energy", "Unit 5 Earth and space"], span=38), 200)
        assert v["suspect"] is False and v["reasons"] == []

    def test_unbookmarked_back_matter_shape_passes(self):
        # 10x20 with the bounded last chapter absorbing 40 extra pages —
        # ratio 3x, under the 4.0 bar (the shape _bounded_last_chapter makes).
        chapters = _flat([f"Unit {i + 1} Plants and animals" for i in range(10)])
        chapters[-1]["end_page"] = 239
        assert assess_chapter_map(chapters, 300)["suspect"] is False

    def test_a_short_list_is_never_judged(self):
        # Two chapters called 'Revision 1' / 'Revision 2' are ordinary; every
        # signal needs enough members to be a pattern, not a coincidence.
        v = assess_chapter_map(_flat(["Revision 1", "Revision 2"]), 40)
        assert v["suspect"] is False

    def test_titles_only_lists_are_judged_on_titles_alone(self):
        # book_health passes stored lists that may carry no page bounds.
        v = assess_chapter_map([{"chapter_num": i, "title": f"Unit {i}"} for i in range(12)], 200)
        assert v["suspect"] is False

    def test_serial_labelling_schemes_with_even_spans_are_healthy(self):
        # A multi-word (or long single-word) generic label plus a serial is
        # the CORRECT chapter list of whole book classes — test-prep books,
        # lab manuals, Indonesian modul, Malay, Arabic and Thai textbooks.
        # Family share is 1.0 for every one of them; only geometry (or a
        # second title smell) may turn that into a verdict, and these books
        # have none. Condemning them demoted correct maps, spent a paid
        # detection pass per index, and gated healthy books with an
        # accusatory dialog — the founder's trust failure, inverted.
        for titles in (
            [f"Practice Test {i}" for i in range(1, 7)],
            [f"Experiment {i}" for i in range(1, 16)],
            [f"Kegiatan Belajar {i}" for i in range(1, 13)],          # Indonesian
            [f"Unit Pembelajaran {i}" for i in range(1, 11)],         # Malay
            [f"الوحدة الدراسية {n}" for n in "١ ٢ ٣ ٤ ٥ ٦ ٧ ٨".split()],   # Arabic
            [f"หน่วยการเรียนรู้ที่ {n}" for n in "๑ ๒ ๓ ๔ ๕ ๖ ๗ ๘".split()],  # Thai
            [f"ユニット{i}" for i in range(1, 11)],                    # Japanese
        ):
            v = assess_chapter_map(_flat(titles), len(titles) * 20)
            assert v["suspect"] is False, titles[0]

    def test_the_family_signal_needs_corroboration(self):
        # The same 13 cover-title bookmarks: flat spans → one signal, quiet;
        # the incident's real scanner geometry → corroborated, condemned.
        titles = [f"LEANERS BOOK 8 {i}" for i in range(1, 14)]
        assert assess_chapter_map(_flat(titles), 260)["suspect"] is False
        chapters, total = _sara_geometry(titles)
        assert assess_chapter_map(chapters, total)["suspect"] is True


# ── language-agnostic BY CONSTRUCTION (requirement 5) ────────────────────────


class TestScripts:
    def test_arabic_cover_title_family_is_flagged(self):
        # RTL titles, Arabic-Indic serials (U+0660..): the Sara shape in Arabic.
        titles = [f"كتاب العلوم للصف الثامن {n}"
                  for n in "١ ٢ ٣ ٤ ٥ ٦ ٧ ٨ ٩ ١٠ ١١ ١٢ ١٣".split()]
        chapters, total = _sara_geometry(titles)
        v = assess_chapter_map(chapters, total)
        assert v["suspect"] is True and v["signals"]["family_members"] == 13

    def test_arabic_generic_unit_labels_are_not(self):
        # 'الوحدة ١'.. — the generic single-word label, Arabic edition.
        titles = [f"الوحدة {n}" for n in "١ ٢ ٣ ٤ ٥ ٦".split()]
        assert assess_chapter_map(_flat(titles), 120)["suspect"] is False

    def test_devanagari_family_and_generic(self):
        junk = [f"विज्ञान पाठ्यपुस्तक कक्षा ८ {n}"
                for n in "१ २ ३ ४ ५ ६ ७ ८ ९ १० ११ १२ १३".split()]
        chapters, total = _sara_geometry(junk)
        assert assess_chapter_map(chapters, total)["suspect"] is True
        healthy = ["अध्याय १ सजीव जगत", "अध्याय २ पदार्थ", "अध्याय ३ बल और गति",
                   "अध्याय ४ ऊर्जा", "अध्याय ५ प्रकाश"]
        assert assess_chapter_map(_flat(healthy), 150)["suspect"] is False

    def test_cjk_cover_title_families_are_caught_at_realistic_lengths(self):
        # CJK writes without spaces, so the 2-token rule can't see the stem —
        # an unbroken spaceless run counts instead, from FOUR characters:
        # real CJK cover titles measure 4-7 ('科学课本' = "science textbook"
        # is 4), and a 10-char bar missed every one of them, replaying the
        # incident silently in Chinese and Japanese.
        for cover in ("科学课本", "八年级科学书", "理科の教科書"):
            chapters, total = _sara_geometry([f"{cover} {i}" for i in range(1, 14)])
            v = assess_chapter_map(chapters, total)
            assert v["suspect"] is True, cover
            assert v["signals"]["family_members"] == 13, cover

    def test_ideographic_numeral_serials_count_as_serials(self):
        # 一二三…十 are category Lo (letters), not Nd/Nl — but they carry a
        # Unicode numeric value, and CJK scanner-typed bookmarks conventionally
        # serialize with them. They must strip exactly as '1..13' do…
        nums = "一 二 三 四 五 六 七 八 九 十 十一 十二 十三".split()
        chapters, total = _sara_geometry([f"中学理科の学習者用ブック {n}" for n in nums])
        v = assess_chapter_map(chapters, total)
        assert v["suspect"] is True and v["signals"]["family_members"] == 13
        # …and BARE ideographic numerals are letterless exactly as '1' is.
        assert assess_chapter_map(_flat(nums), 260)["signals"]["trivial_share"] == 1.0

    def test_leading_serial_families_are_caught(self):
        # 'N - Cover Title' — the other common human enumeration style, and
        # the natural digit-first order for RTL bookmarks. One leading serial
        # run strips exactly as one trailing run does.
        chapters, total = _sara_geometry([f"{i} - LEANERS BOOK 8" for i in range(1, 14)])
        v = assess_chapter_map(chapters, total)
        assert v["suspect"] is True and v["signals"]["family_members"] == 13
        arabic = [f"{n} كتاب العلوم للصف الثامن"
                  for n in "١ ٢ ٣ ٤ ٥ ٦ ٧ ٨ ٩ ١٠ ١١ ١٢ ١٣".split()]
        chapters, total = _sara_geometry(arabic)
        v = assess_chapter_map(chapters, total)
        assert v["suspect"] is True and v["signals"]["family_members"] == 13

    def test_cjk_real_chapter_titles_pass(self):
        healthy = ["第1章 生物のからだ", "第2章 物質の性質", "第3章 力と運動",
                   "第4章 エネルギー", "第5章 地球と宇宙"]
        assert assess_chapter_map(_flat(healthy), 150)["suspect"] is False

    def test_u2160_roman_serials_are_stripped_like_digits(self):
        # Machine-built bookmarks glue U+2160 numerals onto words ('PartⅠ') —
        # the same letter-number class that once blinded _LABEL_RE. As a
        # trailing serial it must unify a family exactly as '1' does.
        titles = [f"LEANERS BOOK 8 {ch}" for ch in "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ"] + ["LEANERS BOOK 8 ⅩⅢ"]
        chapters, total = _sara_geometry(titles)
        v = assess_chapter_map(chapters, total)
        assert v["suspect"] is True and v["signals"]["family_members"] == 13

    def test_arabic_indic_decimal_sections_are_read(self):
        # '١.٣ …' is '1.3 …' — int() understands category-Nd digits, so the
        # decimal-skip signal works without a single ASCII digit.
        titles = ["١.٣ التنفس", "٢.٤ الفصل الكروماتوغرافي", "٤.١ الصحراء",
                  "٥.٣ الطقس والمناخ", "٦.٥ المجرات"]
        v = assess_chapter_map(_flat(titles), 200)
        assert v["signals"]["decimal_nonseq"] is True

    def test_glued_decimal_sections_fire_in_cjk_and_thai(self):
        # CJK and Thai glue the topic straight onto the number — no space —
        # and CJK typography sets the dot fullwidth (．U+FF0E). The old
        # any-letter guard (written for Latin '1.3rd' ordinals) read the topic
        # as an ordinal suffix and made this signal structurally dead in both
        # scripts, so the incident's cosmetically-healed shape re-shipped
        # silently in Chinese or Thai.
        zh = _defs([("1.3光合作用", 0, 5), ("2.4纸层析法", 6, 7), ("4.1沙漠", 8, 21),
                    ("5.3天气与气候", 22, 61), ("6.5星系", 62, 340)])
        v = assess_chapter_map(zh, 341)
        assert v["signals"]["decimal_nonseq"] is True and v["suspect"] is True
        th = _defs([("1.3การหายใจ", 0, 5), ("2.4โครมาโทกราฟี", 6, 7),
                    ("4.1ทะเลทราย", 8, 21), ("6.5กาแล็กซี", 22, 340)])
        assert assess_chapter_map(th, 341)["signals"]["decimal_nonseq"] is True
        fw = _defs([("1．3 光合作用", 0, 5), ("2．4 纸层析法", 6, 7),
                    ("4．1 沙漠", 8, 21), ("6．5 星系", 22, 340)])
        assert assess_chapter_map(fw, 341)["signals"]["decimal_nonseq"] is True
        # …while a genuine Latin ordinal suffix still is not a section number.
        assert assess_chapter_map(
            _flat(["1.3rd Edition Notes", "2.4th Printing", "4.1st Draft"]), 100
        )["signals"]["decimal_nonseq"] is False

    def test_letterless_titles_count_as_trivial_in_any_script(self):
        v = assess_chapter_map(_flat(["1", "٢", "३", "Photosynthesis", "Cells", "Forces"]), 120)
        assert v["signals"]["trivial_share"] == 0.5


# ── the individual signals ───────────────────────────────────────────────────


class TestSignals:
    def test_decimal_sections_with_contiguous_numbering_do_not_fire(self):
        # A deliberate section-level split (1.1, 1.2, 2.1, 2.2) is a different
        # question — _depth_is_sections' job at outline level, not junk.
        titles = ["1.1 Cells", "1.2 Tissues", "2.1 Atoms", "2.2 Molecules"]
        assert assess_chapter_map(_flat(titles), 120)["signals"]["decimal_nonseq"] is False

    def test_span_variance_needs_corroboration(self):
        # The incident's geometry: 2-page and 85-page 'chapters' in one map,
        # max_share 0.249 — the near-miss of _map_shape's AND with 0.25. Alone
        # it is one WEAK signal, never a verdict: healthy titles + wild spans
        # must not condemn a book on geometry a second time.
        chapters, total = _sara_raw()
        v = assess_chapter_map(chapters, total)
        assert v["signals"]["span_variance"] is True
        healthy_titles = _defs([
            ("Introduction", 0, 5), ("The cell", 6, 7), ("Plants", 8, 21),
            ("Animals", 22, 61), ("Ecology", 62, 127), ("Matter", 128, 165),
            ("Forces", 166, 177), ("Energy", 178, 193), ("Sound", 194, 197),
            ("Light", 198, 221), ("Space", 222, 240), ("Earth", 241, 325),
            ("Skills", 326, 340)])
        v2 = assess_chapter_map(healthy_titles, total)
        assert v2["signals"]["span_variance"] is True and v2["suspect"] is False

    def test_uniform_spans_do_not_fire(self):
        chapters = _flat([f"Unit {i + 1} Forces" for i in range(9)], span=38)
        assert assess_chapter_map(chapters, 344)["signals"]["span_variance"] is False


# ── coverage helpers ─────────────────────────────────────────────────────────


class TestCoverage:
    def test_union_counts_overlaps_once(self):
        chapters = _defs([("A", 0, 10), ("B", 5, 20), ("C", 30, 39)])
        assert covered_page_count(chapters, 50) == 31  # 0-20 and 30-39

    def test_head_is_reported_but_separate(self):
        # Front matter before chapter 1 is normal book anatomy — reported so a
        # caller CAN look, never summed into holes or tail.
        chapters = _defs([("A", 10, 19), ("B", 20, 49)])
        gaps = uncovered_pages(chapters, 50)
        assert gaps == {"head": 10, "holes": 0, "tail": 0}

    def test_pageless_lists_make_no_claim(self):
        assert uncovered_pages([{"title": "A"}], 100) == {"head": 0, "holes": 0, "tail": 0}


# ── book_health integration: the gate holds the line (requirements 3+4) ─────


def _extraction(total_pages, readability=0.0, text=""):
    items = [SimpleNamespace(text=text)] if text else []
    return SimpleNamespace(total_pages=total_pages, readability_score=readability,
                           items=items, toc=None)


class TestHealthIntegration:
    def test_a_suspect_list_gates_and_scores_honestly(self):
        chapters, total = _sara_raw()
        h = compute_book_health(_extraction(total), chapters)
        assert h["gate"] == "confirm"
        assert h["band"] not in ("good", "excellent")
        assert any("chapter list" in p.lower() for p in h["problems"])
        assert h["facts"]["chapter_quality"]["suspect"] is True
        # The reassuring "works well" note must never render on a gated book.
        assert h["note"] is None
        assert h["recommendation"] is not None

    def test_the_stored_incident_list_reports_its_hole(self):
        # facts.unmapped_pages printed 0 in production with 36 pages orphaned.
        chapters, total = _sara_stored()
        h = compute_book_health(_extraction(total), chapters)
        assert h["facts"]["unmapped_pages"] == 36
        assert h["facts"]["unmapped_mid_pages"] == 36
        assert h["gate"] == "confirm"
        assert any("aren't covered by any chapter" in p for p in h["problems"])

    def test_a_mid_book_hole_alone_gates(self):
        # Healthy titles, one 40-page hole: low-confidence boundaries even
        # though every title reads fine.
        chapters = _defs([("Cells", 0, 49), ("Plants", 90, 149), ("Forces", 150, 199)])
        h = compute_book_health(_extraction(200, 0.9, "prose " * 500), chapters)
        assert h["facts"]["unmapped_mid_pages"] == 40
        assert h["gate"] == "confirm"

    def test_surviving_suspect_markers_reach_the_gate(self):
        # heal_chapter_boundaries stamps relocation="suspect" on chapters it
        # flagged but could not repair; the store step used to DROP the marker.
        chapters = _flat([f"Unit {i + 1} Forces and motion" for i in range(6)], span=30)
        chapters[2]["relocation"] = "suspect"
        chapters[4]["relocation"] = "suspect"
        h = compute_book_health(_extraction(180, 0.9, "prose " * 500), chapters)
        assert h["gate"] == "confirm"
        assert h["facts"]["suspect_chapters"] == 2
        assert any("couldn't be repaired" in p for p in h["problems"])
        # ONE suspect chapter warns but does not gate the whole book.
        chapters[4].pop("relocation")
        h1 = compute_book_health(_extraction(180, 0.9, "prose " * 500), chapters)
        assert h1["gate"] == "none"
        assert any("couldn't be repaired" in p for p in h1["problems"])

    def test_a_serial_labelled_book_is_not_gated(self):
        # The other half of the family-corroboration rule, at the health
        # level: a lab manual's correct 'Experiment 1..15' map (and every
        # other serial labelling scheme) must keep its clean bill — even
        # though its family share is 1.0 — because health re-judges STORED
        # titles forever: a family-alone verdict here gated such books
        # permanently, whatever the detector stored.
        chapters = _flat([f"Experiment {i}" for i in range(1, 16)], span=10)
        h = compute_book_health(_extraction(150, 0.9, "prose " * 500), chapters)
        assert h["gate"] == "none"
        assert h["band"] == "excellent"
        assert h["problems"] == []
        assert h["facts"]["chapter_quality"]["suspect"] is False

    def test_a_large_unmapped_tail_gates(self):
        # The vision-rescue exit on a long scan: the detector's page window
        # covers the first ~third and the last found unit is clipped, leaving
        # a huge unmapped TAIL — the incident's own book measured 110 of 341
        # pages (32%) uncovered on this exact exit, with 27 estimated parts
        # behind "Generate all" and no dialog. Past the same 25% share that
        # caps the score, the gate must hold the line too (and with it the
        # note suppression and the low_confidence part marker, which both key
        # off the gate).
        chapters = _defs([("1 Respiration and breathing", 6, 47),
                          ("2 Properties of materials", 48, 95),
                          ("3 Forces and motion", 96, 230)])
        h = compute_book_health(_extraction(341), chapters)
        assert h["facts"]["unmapped_pages"] == 110
        assert h["gate"] == "confirm"
        assert h["note"] is None
        assert h["band"] not in ("good", "excellent")
        assert any("aren't covered by any chapter" in p for p in h["problems"])
        # A normal unbookmarked back-matter tail stays ungated.
        ok = _flat([f"Unit {i + 1} Living things" for i in range(9)], span=34)
        h2 = compute_book_health(_extraction(344), ok)  # 38-page tail, 11%
        assert h2["gate"] == "none"

    def test_a_healthy_scanned_book_keeps_its_note_and_band(self):
        # The other side of note suppression: an ordinary scan with a sound
        # chapter list still reads "good" with the informational note.
        chapters = _flat([f"Unit {i + 1} Living things" for i in range(9)], span=38)
        h = compute_book_health(_extraction(344), chapters)
        assert h["gate"] == "none"
        assert h["band"] == "good"
        assert h["note"] and "vision" in h["note"].lower()
        assert h["facts"]["chapter_quality"]["suspect"] is False

    def test_health_stays_json_serializable(self):
        chapters, total = _sara_stored()
        json.dumps(compute_book_health(_extraction(total), chapters))
