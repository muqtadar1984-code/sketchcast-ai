"""THE FLEET AUDIT (2026-08-23/24) — every live defect class, pinned on the
real library's own titles and ranges.

After the Sara incident the whole live library was audited: 14 books carrying
genuine defects — most at score 95, six of them with real generations burned —
and 7 healthy books that must never regress. The defective shapes are stored
verbatim in tests/fixtures/fleet_audit_2026_08_23.json and pinned here against
the outcome validator (chapter_quality.assess_chapter_map) and the health
gate. The classes:

  empty_titles        948a9494 — 9 of 10 titles are the EMPTY STRING
  duplicate/fragment  bb68dec6 — one title 3x, plus 'things'/'enough'
  filename_titles     2ea65b58 — '031-072-C606' page-range slugs
                      994b8238 — export slugs AND end<start AND overlap ranges
  invalid_ranges      9c36b003 — end_page -1, stored in prod
  blank_line_titles   f22b8b64 — ': ____________' Jawi worksheet lines
  ocr_mojibake        81c94f98 — 'األأ' lam-alef, harakat on punctuation
  replacement_char    eb51a014 — U+FFFD inside a title at score 95
  section_promotion   f455c5bd — unit titles beside '2.2..2.6' sections
  duplicate_content   682eedf5 — 'Communication' AND 'Unit 2: Communication'
  glued_body_text     a13a45e2 / 4e66897c — heading + first body sentence

Structural checks are script-agnostic; every lexical predicate carries
non-Latin coverage here (the audit's own defects are half Arabic).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent1_ingestion.book_health import compute_book_health
from agent1_ingestion.chapter_quality import (_carries_body_text, _is_garbled,
                                              _is_lowercase_fragment,
                                              _looks_like_filename,
                                              assess_chapter_map)

FIXTURE = Path(__file__).parent / "fixtures" / "fleet_audit_2026_08_23.json"


def _fleet():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _entry(book_prefix: str) -> dict:
    for e in _fleet()["defective"]:
        if e["book"].startswith(book_prefix):
            return e
    raise AssertionError(f"fixture entry {book_prefix} missing")


def _defs(titles, span=20, ranges=None):
    if ranges is not None:
        return [{"chapter_num": i, "title": t, "start_page": s, "end_page": e}
                for i, (t, (s, e)) in enumerate(zip(titles, ranges))]
    return [{"chapter_num": i, "title": t, "start_page": i * span,
             "end_page": i * span + span - 1} for i, t in enumerate(titles)]


def _extraction(total_pages):
    return SimpleNamespace(total_pages=total_pages, readability_score=0.0,
                           items=[], toc=None)


def _gates(chapters, total_pages):
    h = compute_book_health(_extraction(total_pages), chapters)
    return h["gate"] == "confirm" and h["band"] not in ("good", "excellent")


# ── the defective fleet, class by class ──────────────────────────────────────


class TestEmptyTitles:
    def test_948a9494_nine_empty_titles_condemn_alone(self):
        # 'Grade 12 Physics': 9 of 10 titles empty, first chapter 'Contents',
        # shipped at 95 and burned 6 generations. An empty title is not a
        # label anyone typed — a HARD signal, no second smell required.
        titles = ["Contents"] + [""] * 9
        v = assess_chapter_map(_defs(titles, span=25), 250)
        assert v["suspect"] is True
        assert v["signals"]["empty_titles"] == 9
        assert _gates(_defs(titles, span=25), 250)

    def test_one_stray_empty_title_is_a_fluke_not_a_verdict(self):
        titles = ["Cells", "", "Forces", "Energy", "Waves", "Light"]
        assert assess_chapter_map(_defs(titles), 120)["suspect"] is False


class TestDuplicateAndFragmentTitles:
    def test_bb68dec6_the_real_titles_are_condemned(self):
        e = _entry("bb68dec6")
        v = assess_chapter_map(_defs(e["titles"]), 100)
        assert v["suspect"] is True
        assert v["signals"]["duplicate_titles"] >= 2   # one title stored 3x
        assert v["signals"]["fragment_titles"] == 2    # 'things', 'enough'
        assert _gates(_defs(e["titles"]), 100)

    def test_fragments_are_cased_script_only(self):
        # Arabic has no case: a short Arabic title must never read as a
        # 'lowercase fragment'.
        assert _is_lowercase_fragment("things") is True
        assert _is_lowercase_fragment("الضوء") is False
        assert _is_lowercase_fragment("光") is False
        # …and an all-lowercase STYLED book (every title lowercase) is
        # styling, not junk: the signal needs a minority.
        titles = ["cells", "forces", "energy", "waves", "light", "sound"]
        v = assess_chapter_map(_defs(titles), 120)
        assert v["signals"]["fragment_titles"] == 6 and v["suspect"] is False


class TestFilenameTitles:
    def test_2ea65b58_page_range_slugs_are_condemned(self):
        # The علوم book — the exact class _looks_like_file_bookmark existed
        # for and missed (extension already stripped). 7 generations burned.
        e = _entry("2ea65b58")
        v = assess_chapter_map(_defs(e["titles"], span=30), 130)
        assert v["suspect"] is True
        assert v["signals"]["filename_titles"] == 4
        assert _gates(_defs(e["titles"], span=30), 130)

    def test_994b8238_export_slugs_and_corrupt_ranges(self):
        # Asset file names AND end_page < start_page AND a [2,127] span
        # overlapping everything — two independent HARD signals in one row.
        e = _entry("994b8238")
        titles = [e["titles"][0]] + [f"esl_cie_asl_genpaper_1ed_tr_ch1.{i}_wksht_TOR"
                                     for i in range(2, 6)]
        chapters = _defs(titles, ranges=[tuple(r) for r in e["ranges"]])
        v = assess_chapter_map(chapters, 128)
        assert v["suspect"] is True
        assert v["signals"]["filename_titles"] == 5
        assert v["signals"]["invalid_ranges"] is True
        assert _gates(chapters, 128)

    def test_the_no_real_word_rule_is_latin_only(self):
        # A spaceless-script title packs a word per character: '第7章' has two
        # letters and is a perfectly good chapter title, and Arabic titles
        # with digits are ordinary. Only Latin slugs are judged wordless.
        assert _looks_like_filename("031-072-C606") is True
        assert _looks_like_filename("000 C606") is True
        assert _looks_like_filename("第7章") is False
        assert _looks_like_filename("الوحدة 12") is False
        assert _looks_like_filename("Unit 12") is False  # 'Unit' is a word

    def test_literal_extensions_and_underscore_slugs(self):
        assert _looks_like_filename("chapter_final_v2.pdf") is True
        assert _looks_like_filename("esl_cie_asl_genpaper_1ed_tr_TOR") is True
        assert _looks_like_filename("Forces and motion") is False


class TestInvalidRanges:
    def test_9c36b003_end_page_minus_one_condemns_alone(self):
        # Stored in prod; 18 generations burned on this shape. The structurer
        # repairs before judging, so a fresh map never shows these — but
        # book_health judges STORED rows forever.
        e = _entry("9c36b003")
        chapters = _defs(["Price elasticity of supply", "Price elasticity of supply"],
                         ranges=[tuple(r) for r in e["ranges"]])
        v = assess_chapter_map(chapters, 40)
        assert v["suspect"] is True
        assert v["signals"]["invalid_ranges"] is True

    def test_overlapping_ranges_are_corrupt(self):
        chapters = _defs(["Cells", "Forces", "Energy"],
                         ranges=[(0, 20), (10, 30), (31, 39)])
        assert assess_chapter_map(chapters, 40)["signals"]["invalid_ranges"] is True

    def test_sound_ranges_are_not(self):
        chapters = _defs(["Cells", "Forces", "Energy"],
                         ranges=[(0, 12), (13, 27), (28, 39)])
        assert assess_chapter_map(chapters, 40)["signals"]["invalid_ranges"] is False


class TestBlankLineTitles:
    def test_f22b8b64_jawi_worksheet_blanks_are_condemned(self):
        # The founder's other-languages fear made real: fill-in-the-blank
        # underscore lines read as chapter titles in an RTL/Jawi workbook.
        e = _entry("f22b8b64")
        titles = e["titles"] + ["Tajwid", "Latihan"]
        v = assess_chapter_map(_defs(titles), 80)
        assert v["suspect"] is True
        assert v["signals"]["blank_line_titles"] == 2
        assert _gates(_defs(titles), 80)

    def test_an_underscore_inside_a_real_name_is_not_a_blank(self):
        v = assess_chapter_map(_defs(["snake_case_style", "Forces", "Energy"]), 60)
        assert v["signals"]["blank_line_titles"] == 0


class TestArabicMojibake:
    def test_81c94f98_the_real_titles_are_condemned(self):
        # 24 generations burned by an active user on 'رياضيات رابع'. 'األأ' is
        # the classic cp1256 lam-alef round-trip; a damma sits on the question
        # mark; a kasra opens a title.
        e = _entry("81c94f98")
        titles = e["titles"] + ["الأعداد", "القياس"]
        v = assess_chapter_map(_defs(titles), 100)
        assert v["suspect"] is True
        assert v["signals"]["garbled_titles"] >= 2
        assert _gates(_defs(titles), 100)

    def test_the_predicates_read_codepoints_not_vibes(self):
        assert _is_garbled("ما األأنماطُ؟ وما الدَّوالُّ؟") is True   # اأ inside a word
        assert _is_garbled("الْمَنْزِلِيَّةُ؟ُمَا الْقِيمَة") is True  # damma on '؟'
        assert _is_garbled("ِأُجرِيَ مسحٌ") is True                  # leading kasra
        # Correctly vocalised Arabic — harakat all on letters — must pass:
        assert _is_garbled("الْمَمْلَكَةُ الْحَيَوَانِيَّةُ") is False
        assert _is_garbled("ما الأنماط؟ وما الدوال؟") is False
        # …and Devanagari matras on their consonants too (category Mn/Mc mix).
        assert _is_garbled("विज्ञान की दुनिया") is False


class TestReplacementChar:
    def test_eb51a014_one_ufffd_condemns_alone(self):
        # The cheapest possible check, and a live book carried one at 95: a
        # replacement character is a decode failure BY DEFINITION.
        e = _entry("eb51a014")
        titles = e["titles"] + ["مهارات الحياة", "التواصل", "التعاون"]
        v = assess_chapter_map(_defs(titles), 80)
        assert v["suspect"] is True
        assert v["signals"]["replacement_char"] is True
        assert _gates(_defs(titles), 80)


class TestSectionPromotion:
    def test_f455c5bd_units_interleaved_with_sections(self):
        # Cambridge Science LB7 — same series as Sara's LB8: 'Cells' beside
        # '1.4 Cells, tissues and organs', 2.1/3.1 missing — 15 'chapters' for
        # a 9-unit book. Two weak signals corroborate: mixed levels + the
        # minors of each major not starting at .1.
        e = _entry("f455c5bd")
        v = assess_chapter_map(_defs(e["titles"], span=12), 190)
        assert v["suspect"] is True
        assert v["signals"]["mixed_levels"] is True
        assert v["signals"]["decimal_nonseq"] is True
        assert _gates(_defs(e["titles"], span=12), 190)

    def test_a_deliberate_full_section_split_is_not_condemned(self):
        # 1.1, 1.2, 2.1, 2.2 with no plain titles mixed in and no gaps is a
        # different question — kept, whatever one thinks of the altitude.
        titles = ["1.1 Cells", "1.2 Tissues", "2.1 Atoms", "2.2 Molecules",
                  "3.1 Forces", "3.2 Motion"]
        assert assess_chapter_map(_defs(titles), 120)["suspect"] is False


class TestDuplicateContent:
    def test_682eedf5_prefix_duplicates_and_numbering_gaps(self):
        # 'Communication' AND 'Unit 2: Communication' both stored; Unit 3
        # absent from the sequence.
        e = _entry("682eedf5")
        v = assess_chapter_map(_defs(e["titles"], span=12), 100)
        assert v["suspect"] is True
        assert v["signals"]["duplicate_titles"] >= 1
        assert v["signals"]["label_number_gaps"] is True
        assert _gates(_defs(e["titles"], span=12), 100)

    def test_a_stored_list_starting_at_chapter_2_is_not_a_gap(self):
        # Detection missing the FRONT is a different defect with a different
        # guardrail; contiguity is judged from wherever the run starts.
        titles = ["Unit 2: Communication", "Unit 3: Food", "Unit 4: Media"]
        assert assess_chapter_map(_defs(titles), 60)["signals"]["label_number_gaps"] is False


class TestGluedBodyText:
    def test_a13a45e2_the_real_titles_are_condemned(self):
        # 'Enterprise This chapter covers syllabus section AS Level 1.1' — 37
        # of these. The two stored samples plus siblings of the same shape.
        e = _entry("a13a45e2")
        titles = list(e["titles"]) + [
            f"{t} This chapter covers syllabus section AS Level 1.{i + 2}"
            for i, t in enumerate(["Business structure", "Size of business",
                                   "Business objectives"])]
        v = assess_chapter_map(_defs(titles, span=18), 100)
        assert v["suspect"] is True
        assert v["signals"]["glued_titles"] == len(titles)
        assert _gates(_defs(titles, span=18), 100)

    def test_4e66897c_sentence_runs_on_into_the_title(self):
        e = _entry("4e66897c")
        v = assess_chapter_map(_defs(list(e["titles"]) * 2, span=12), 100)
        assert v["suspect"] is True
        assert v["signals"]["glued_titles"] == 4

    def test_the_predicate_reads_cjk_and_arabic_sentence_ends(self):
        assert _carries_body_text("光合作用 植物は光をエネルギーに変える。次の節では") is True
        assert _carries_body_text("التنفس تحتاج الكائنات الحية إلى الطاقة؟ وفي هذا الفصل") is True
        # Real long-ish titles with no run-on stay clean:
        assert _carries_body_text("Cells, tissues and organ systems") is False
        assert _carries_body_text("AS Level 1.1 Enterprise and business") is False

    def test_a_glued_minority_is_one_smell_not_a_verdict(self):
        titles = ["MAJOR LANDFORMS OF THE EARTH You must have seen some of the landform features as",
                  "Water", "Air", "Maps", "Our Country", "The Earth", "Motions", "Globe"]
        v = assess_chapter_map(_defs(titles, span=12), 100)
        assert v["signals"]["glued_titles"] == 1
        assert v["suspect"] is False


# ── the healthy seven must pass untouched ────────────────────────────────────


class TestHealthyFleet:
    """One map per healthy library book, shaped from its audit note. Every
    one must keep suspect=False, gate 'none' and an empty problems list —
    these are live teachers' working books."""

    CASES = {
        # c1ffabda / 4fc89a7e — the canonical good Cambridge book.
        "cambridge_y7": (
            ["Plants and animals", "Rocks and soils", "States of matter",
             "Sound", "Light and shadows", "Magnets and springs",
             "Habitats", "Keeping healthy", "Earth and beyond"], 38),
        # 07c49ee4 — clean bilingual Arabic/English titles.
        "arabic_bilingual_physics": (
            ["الفصل الأول الحركة الموجية Wave Motion",
             "الفصل الثاني الصوت Sound",
             "الفصل الثالث الضوء Light",
             "الفصل الرابع المرايا Mirrors",
             "الفصل الخامس العدسات Lenses"], 30),
        # 7bbcf45b — clean Arabic titles, scanned book.
        "arabic_reading_grade1": (
            ["أسرتي", "مدرستي", "الحيوانات", "النباتات", "وطني", "الألوان"], 20),
        # a047c17a — Turkish, dotted-capital İ, KISIM/BÖLÜM structure.
        "turkish_anayasa": (
            ["BİRİNCİ KISIM Genel Esaslar",
             "İKİNCİ KISIM Temel Haklar ve Ödevler",
             "ÜÇÜNCÜ KISIM Cumhuriyetin Temel Organları",
             "DÖRDÜNCÜ KISIM Mali ve Ekonomik Hükümler",
             "BEŞİNCİ KISIM Çeşitli Hükümler"], 24),
        # 5bd0d381 — PartⅠ-Ⅶ with U+2160 roman numerals.
        "business_dynamics": (
            ["PartⅠ Perspective and Process",
             "PartⅡ Tools for Systems Thinking",
             "PartⅢ The Dynamics of Growth",
             "PartⅣ Tools for Modeling Dynamic Systems",
             "PartⅤ Instability and Oscillation",
             "PartⅥ Supply Chains and Boom and Bust",
             "PartⅦ Robust Workhorses"], 144),
        # c908cc53 — Malay exam-paper compilation; unusual but correct.
        "malay_pai": (
            ["Soalan Percubaan 1", "Soalan Percubaan 2", "Soalan Percubaan 3",
             "Soalan Percubaan 4", "Nota Ringkas Akidah",
             "Nota Ringkas Ibadah"], 15),
        # bf80cbc9 / 670defb0 — scanned, clean vision-read titles.
        "grade5_scanned": (
            ["Living Things", "The Environment", "Matter", "Energy",
             "Our Earth"], 28),
    }

    def test_every_healthy_shape_stays_clean(self):
        for name, (titles, span) in self.CASES.items():
            chapters = _defs(titles, span=span)
            total = len(titles) * span
            v = assess_chapter_map(chapters, total)
            assert v["suspect"] is False, (name, v["reasons"])
            h = compute_book_health(_extraction(total), chapters)
            assert h["gate"] == "none", (name, h["problems"])
            assert h["facts"]["chapter_quality"]["suspect"] is False, name


class TestFixtureIsGroundTruth:
    def test_the_fixture_carries_the_audit(self):
        d = _fleet()
        assert len(d["defective"]) == 14
        assert len(d["healthy_must_not_regress"]) == 7
        founder = _entry("FOUNDER UPLOAD")
        assert len(founder["true_units"]) == 9
        assert founder["true_unit_start_pages_1based"][0] == 9
