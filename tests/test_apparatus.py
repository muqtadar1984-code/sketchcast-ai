"""APPARATUS IS NOT A CHAPTER (founder decision, 2026-08-24).

"A chapter is a unit of the book's TEACHING SEQUENCE." Cover, contents,
copyright, acknowledgements, glossary, index, answer keys and reference/skills
sections (Cambridge 'Science Skills' — founder explicit) are excluded from the
chapter list outright: never listed, never gated as chapters, never
part-split into credit rows — and RECORDED, never vanished, so the
unmapped-pages guardrail can tell a deliberate exclusion from a detection
hole.

The motivating book is the founder's own upload (2026-08-23): the dokumen.pub
Cambridge LB8 scan, 339 pages, zero text layer, whose hand-made bookmarks
stored Cover Page, Contents, Science Skills, and Glossary and Index as
top-level chapters — the Glossary split into THREE one-credit parts. The
end-to-end here drives the vision pipeline (fake render, scripted client
replies in the documented JSON shapes) through the window scan + contents-page
route on exactly that book's geometry and requires EXACTLY the 9 numbered
units out the other side.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import agent1_ingestion.vision_chapters as vc
from agent1_ingestion.book_health import compute_book_health
from agent1_ingestion.chapter_quality import (apparatus_kind, split_apparatus,
                                              uncovered_pages)
from agent1_ingestion.structurer import structure_book

FLEET = Path(__file__).parent / "fixtures" / "fleet_audit_2026_08_23.json"


def _founder():
    d = json.loads(FLEET.read_text(encoding="utf-8"))
    return next(e for e in d["defective"] if e["book"].startswith("FOUNDER"))


# ── the conservative lexical trim ────────────────────────────────────────────


class TestApparatusKind:
    def test_the_multilingual_lexicon(self):
        # Front matter near the front; back matter near the back — the named
        # languages from the ruling, plus the ones the library actually holds.
        front = [("Cover Page", "cover"), ("Contents", "contents"),
                 ("المحتويات", "contents"), ("الفهرس", "contents"),
                 ("فهرس", "contents"), ("目录", "contents"), ("目次", "contents"),
                 ("İçindekiler", "contents"), ("Daftar Isi", "contents"),
                 ("अनुक्रमणिका", "contents"), ("สารบัญ", "contents"),
                 ("Copyright", "imprint"), ("Acknowledgements", "acknowledgements")]
        for title, kind in front:
            assert apparatus_kind(title, 2, 300) == kind, title
        back = [("Glossary", "glossary"), ("مسرد", "glossary"),
                ("Glosario", "glossary"), ("Sözlük", "glossary"),
                ("词汇表", "glossary"), ("用語集", "glossary"),
                ("Index", "index"), ("索引", "index"), ("अनुक्रमणिका", "index"),
                ("Answers", "answers"), ("مفتاح الإجابات", "answers"),
                ("เฉลย", "answers"), ("References", "references")]
        for title, kind in back:
            assert apparatus_kind(title, 285, 300) == kind, title

    def test_position_gates_protect_real_chapters(self):
        # A mid-book chapter that happens to carry an apparatus word is KEPT —
        # over-trimming a real chapter is worse than showing one glossary.
        assert apparatus_kind("Contents", 150, 300) is None      # front word, mid-book
        assert apparatus_kind("Glossary", 100, 300) is None      # back word, mid-book
        assert apparatus_kind("Index", 30, 300) is None
        # …and whole-title matching, never substrings.
        assert apparatus_kind("Answers to big questions", 285, 300) is None
        assert apparatus_kind("The Glossary of Life", 285, 300) is None

    def test_introduction_is_deliberately_not_apparatus(self):
        # An introduction is routinely the first real teaching unit.
        assert apparatus_kind("Introduction", 2, 300) is None
        assert apparatus_kind("مقدمة", 2, 300) is None

    def test_composite_back_matter(self):
        assert apparatus_kind("Glossary and Index", 285, 300) == "glossary"
        # Every joined part must itself be back matter, or the entry is kept.
        assert apparatus_kind("Glossary and Beyond", 285, 300) is None

    def test_science_skills_is_founder_explicit_and_hard_against_the_back(self):
        assert apparatus_kind("Science Skills", 322, 339) == "reference"
        # …but a '* Skills' title anywhere else could be a real unit: kept.
        assert apparatus_kind("Science Skills", 200, 339) is None
        assert apparatus_kind("Thinking and Working Scientifically Skills", 322, 339) is None

    def test_dotted_contents_lines_normalise(self):
        # 'Contents ......... 4' — the shape a contents ENTRY title takes.
        assert apparatus_kind("Contents ......... 4", 3, 300) == "contents"


class TestSplitApparatus:
    def test_kept_units_are_renumbered_and_apparatus_recorded(self):
        chapters = [
            {"chapter_num": 0, "title": "Cover Page", "start_page": 0, "end_page": 1},
            {"chapter_num": 1, "title": "Contents", "start_page": 2, "end_page": 4},
            {"chapter_num": 2, "title": "Respiration", "start_page": 5, "end_page": 150},
            {"chapter_num": 3, "title": "Magnetism", "start_page": 151, "end_page": 285},
            {"chapter_num": 4, "title": "Glossary and Index", "start_page": 286, "end_page": 299},
        ]
        kept, cut = split_apparatus(chapters, 300)
        assert [c["title"] for c in kept] == ["Respiration", "Magnetism"]
        assert [c["chapter_num"] for c in kept] == [0, 1]  # contiguous renumber
        assert [(a["kind"], a["start_page"]) for a in cut] == [
            ("cover", 0), ("contents", 2), ("glossary", 286)]

    def test_a_detector_kind_marker_is_honoured_in_any_language(self):
        # The vision/text-LLM rungs classify semantically and stamp
        # kind="apparatus" — the lexicon need not know the word.
        chapters = [
            {"chapter_num": 0, "title": "Isi Kandungan Buku", "start_page": 0,
             "end_page": 3, "kind": "apparatus"},
            {"chapter_num": 1, "title": "Respirasi", "start_page": 4, "end_page": 150,
             "kind": "unit"},
            {"chapter_num": 2, "title": "Senarai Istilah Penting", "start_page": 151,
             "end_page": 199, "kind": "apparatus"},
        ]
        kept, cut = split_apparatus(chapters, 200)
        assert [c["title"] for c in kept] == ["Respirasi"]
        # The kind key is dropped from kept units — the stored chapter shape
        # is byte-compatible with what every consumer already reads.
        assert "kind" not in kept[0]
        assert len(cut) == 2 and all(a["kind"] == "apparatus" for a in cut)

    def test_refuses_to_empty_the_list(self):
        # A map that is ALL apparatus is a detection failure; returning it
        # untrimmed keeps the failure visible to the validator.
        chapters = [
            {"chapter_num": 0, "title": "Contents", "start_page": 0, "end_page": 4},
            {"chapter_num": 1, "title": "Index", "start_page": 280, "end_page": 299},
        ]
        kept, cut = split_apparatus(chapters, 300)
        assert len(kept) == 2 and cut == []

    def test_when_in_doubt_keep(self):
        chapters = [
            {"chapter_num": 0, "title": "Answers in the natural world", "start_page": 0, "end_page": 99},
            {"chapter_num": 1, "title": "Reference frames", "start_page": 100, "end_page": 199},
            {"chapter_num": 2, "title": "Skills for life", "start_page": 200, "end_page": 299},
        ]
        kept, cut = split_apparatus(chapters, 300)
        assert len(kept) == 3 and cut == []


class TestCoverageAccounting:
    def test_recorded_apparatus_is_not_a_hole(self):
        chapters = [{"chapter_num": 0, "title": "U1", "start_page": 8, "end_page": 150},
                    {"chapter_num": 1, "title": "U2", "start_page": 151, "end_page": 320}]
        apparatus = [{"title": "Cover", "start_page": 0, "end_page": 7, "kind": "cover"},
                     {"title": "Glossary and Index", "start_page": 321, "end_page": 338,
                      "kind": "glossary"}]
        gaps = uncovered_pages(chapters, 339, apparatus=apparatus)
        assert gaps == {"head": 8, "holes": 0, "tail": 0}
        # Only the RECORD buys the pardon — unrecorded gaps keep counting.
        assert uncovered_pages(chapters, 339)["tail"] == 18

    def test_health_stamps_the_record_and_pardons_the_tail(self):
        ext = SimpleNamespace(total_pages=339, readability_score=0.0, items=[], toc=None)
        chapters = [{"chapter_num": i, "title": t, "start_page": 8 + i * 35,
                     "end_page": 8 + i * 35 + 34} for i, t in enumerate(
                        ["Respiration", "Materials", "Forces", "Ecosystems",
                         "Cycles", "Light", "Diet", "Reactions", "Magnetism"])]
        apparatus = [{"title": "Science Skills", "start_page": 323, "end_page": 326,
                      "kind": "reference"},
                     {"title": "Glossary and Index", "start_page": 327, "end_page": 338,
                      "kind": "glossary"}]
        h = compute_book_health(ext, chapters, apparatus=apparatus)
        assert h["facts"]["apparatus"] == apparatus
        assert h["facts"]["unmapped_pages"] == 0
        assert h["gate"] == "none"
        # Without the record the same 16-page tail counts (though it stays
        # under every gate) — the pardon is the record's doing, not slack.
        h2 = compute_book_health(ext, chapters)
        assert h2["facts"]["unmapped_pages"] == 16


# ── the founder's book, end to end through the vision pipeline ───────────────


def _fake_render(pdf, pages, width, out_dir):
    return [Path(f"p{p:04d}.jpg") for p in pages]


def _page_of(path) -> int:
    return int(Path(path).stem[1:])


class FounderBookClient:
    """Scripted replies in the documented JSON shapes for the dokumen.pub LB8
    geometry: 339 pages, units at 0-based 8/40/68/126/152/190/233/263/293,
    Science Skills at 321, Glossary and Index at 326 (fleet ground truth).

    The printed contents-page numbers are the REAL ones read off the scan's
    own contents table (pages 5-6 of the PDF): units at printed
    8/40/68/126/152/190/233/263/293, 'Science Skills 321', 'Glossary and
    index 326' — a constant printed→physical offset of 1 (printed N sits on
    physical 0-based N), which is what the calibration must recover from the
    window-scan anchors.
    """

    UNITS = ["Respiration", "Properties of Materials", "Forces and Energy",
             "Ecosystems", "Materials and Cycles on Earth", "Light",
             "Diet and Growth", "Chemical Reactions", "Magnetism"]
    UNIT_STARTS = [8, 40, 68, 126, 152, 190, 233, 263, 293]
    SKILLS_START, GLOSSARY_START = 321, 326
    OFFSET = 1

    def __init__(self, fail_pages=()):
        self.fail_pages = set(fail_pages)  # verification says "no" here
        self.calls = {"window": 0, "contents": 0, "verify": 0}

    def analyze_images_batch(self, paths, prompt, max_tokens=0, **k):
        pages = [_page_of(p) for p in paths]
        if "printed_page" in prompt:  # contents-page transcription
            self.calls["contents"] += 1
            entries = [
                {"number": i + 1, "title": t,
                 "printed_page": self.UNIT_STARTS[i] - self.OFFSET + 1, "kind": "unit"}
                for i, t in enumerate(self.UNITS)
            ] + [
                {"number": None, "title": "Science Skills",
                 "printed_page": self.SKILLS_START - self.OFFSET + 1, "kind": "apparatus"},
                {"number": None, "title": "Glossary and index",
                 "printed_page": self.GLOSSARY_START - self.OFFSET + 1, "kind": "apparatus"},
            ]
            return {"data": {"entries": entries}}
        if "opens_section" in prompt:  # extrapolated-opener verification
            self.calls["verify"] += 1
            rows = [{"image_number": i + 1,
                     "opens_section": pg not in self.fail_pages,
                     "title": ""}
                    for i, pg in enumerate(pages)]
            return {"data": {"pages": rows}}
        # window scan: openers by image position
        self.calls["window"] += 1
        openers = {0: ("Cover", "apparatus"), 4: ("Contents", "apparatus")}
        openers.update({s: (t, "unit") for s, t in zip(self.UNIT_STARTS, self.UNITS)})
        items = [{"image_number": i + 1, "title": openers[pg][0], "kind": openers[pg][1]}
                 for i, pg in enumerate(pages) if pg in openers]
        return {"data": {"openers": items}}


def _scanned_extraction(total_pages=339):
    return SimpleNamespace(toc=[], total_pages=total_pages, items=[],
                           readability_score=0.0, extraction_backend="pymupdf",
                           markdown="")


class TestFounderBook:
    def test_exactly_the_nine_units_and_nothing_else(self, monkeypatch):
        # THE ground-truth contract: 9 numbered units, apparatus recorded,
        # nothing gated. The contents-page route is what carries structure
        # past the 120-page scan window.
        monkeypatch.setattr(vc, "_render_pages", _fake_render)
        client = FounderBookClient()
        fx = _founder()
        ext = _scanned_extraction()
        book = structure_book(
            book_id="founder-lb8", title="Cambridge Lower Secondary Science 8",
            author="CUP", isbn=None, extraction=ext, images=[],
            pdf_path="x.pdf", client=client,
        )
        assert client.calls["contents"] == 1 and client.calls["verify"] >= 1
        assert book.total_chapters == 9
        assert [c.title for c in book.chapters] == FounderBookClient.UNITS
        assert ([c.start_page + 1 for c in book.chapters]
                == fx["true_unit_start_pages_1based"])
        # Unit 9 ends where Science Skills begins — the apparatus boundary
        # bounds the unit even though the apparatus is not a chapter.
        assert book.chapters[-1].end_page == FounderBookClient.SKILLS_START - 1
        kinds = [(a.kind, a.start_page) for a in book.apparatus]
        assert kinds == [("cover", 0), ("contents", 4),
                         ("reference", 321), ("glossary", 326)]
        # …and health reads a clean, ungated, fully-accounted book.
        h = compute_book_health(ext, [
            {"chapter_num": c.chapter_num, "title": c.title,
             "start_page": c.start_page, "end_page": c.end_page}
            for c in book.chapters],
            apparatus=[a.model_dump() for a in book.apparatus])
        assert h["gate"] == "none"
        assert h["facts"]["unmapped_pages"] == 0
        assert h["facts"]["chapter_quality"]["suspect"] is False
        assert len(h["facts"]["apparatus"]) == 4

    def test_without_the_contents_route_the_window_result_gates_honestly(self, monkeypatch):
        # If the contents page cannot be used (here: its printed numbering
        # never calibrates because the window found no anchors — simulated by
        # a client whose contents read returns nothing), the 120-page window
        # yields units 1-3 and health must gate the truncation, never ship
        # 3-of-9 as "done".
        monkeypatch.setattr(vc, "_render_pages", _fake_render)

        class NoContents(FounderBookClient):
            def analyze_images_batch(self, paths, prompt, max_tokens=0, **k):
                if "printed_page" in prompt:
                    return {"data": {"entries": []}}
                return super().analyze_images_batch(paths, prompt, max_tokens, **k)

        ext = _scanned_extraction()
        book = structure_book(
            book_id="founder-lb8", title="Cambridge Lower Secondary Science 8",
            author="CUP", isbn=None, extraction=ext, images=[],
            pdf_path="x.pdf", client=NoContents(),
        )
        assert book.total_chapters == 3  # the honest window partial
        h = compute_book_health(ext, [
            {"chapter_num": c.chapter_num, "title": c.title,
             "start_page": c.start_page, "end_page": c.end_page}
            for c in book.chapters],
            apparatus=[a.model_dump() for a in book.apparatus])
        assert h["gate"] == "confirm"
        assert h["band"] not in ("good", "excellent")
        assert any("aren't covered" in p for p in h["problems"])

    def test_a_failed_verification_drops_the_extrapolated_opener(self, monkeypatch):
        # An extrapolated page that fails the look is dropped — a missing unit
        # gates honestly; a wrong boundary bills kits forever.
        monkeypatch.setattr(vc, "_render_pages", _fake_render)
        client = FounderBookClient(fail_pages={190})  # unit 6's page fails the look
        defs = vc.detect_chapters_vision("x.pdf", 339, client)
        starts = [d["start_page"] for d in defs]
        assert 190 not in starts
        assert 152 in starts and 233 in starts  # neighbours survive

    def test_printed_numbers_that_do_not_calibrate_are_refused(self):
        # Anchors disagreeing on the offset = printed numbering this route
        # refuses to use. Untrusted numbers in, verified positions out.
        entries = [{"number": 1, "title": "Respiration", "printed_page": 6, "kind": "unit"},
                   {"number": 2, "title": "Properties of Materials", "printed_page": 30,
                    "kind": "unit"}]
        found = [(8, "Respiration", "unit"), (40, "Properties of Materials", "unit")]
        assert vc._calibrate_printed_offset(entries, found) is None
        # Two anchors agreeing DO calibrate.
        entries[1]["printed_page"] = 38
        assert vc._calibrate_printed_offset(entries, found) == 3

    def test_starts_to_defs_propagates_the_kind_marker(self):
        pairs = [(0, "Cover", "apparatus"), (8, "Respiration", "unit"),
                 (40, "Materials", None)]
        defs = vc._starts_to_defs(pairs, 100)
        assert defs[0]["kind"] == "apparatus"
        assert "kind" not in defs[1] and "kind" not in defs[2]

    def test_known_chapters_are_never_trimmed(self):
        # Stored boundaries are authoritative: a generation must split the
        # book exactly as indexing stored it, glossary rows included — the
        # trim happens at INDEX time or not at all.
        known = [{"chapter_num": 0, "title": "Respiration", "start_page": 8, "end_page": 320},
                 {"chapter_num": 1, "title": "Glossary and Index", "start_page": 321,
                  "end_page": 338}]
        book = structure_book(
            book_id="b", title="LB8", author="CUP", isbn=None,
            extraction=_scanned_extraction(), images=[], known_chapters=known,
        )
        assert book.total_chapters == 2
        assert book.chapters[1].title == "Glossary and Index"
        assert book.apparatus == []


class TestHealPoolExcludesApparatus:
    def test_a_unit_is_never_relocated_onto_apparatus(self, monkeypatch):
        # heal/generation-time relocation must not offer a glossary or
        # contents page as a destination for a teaching unit.
        monkeypatch.setattr(vc, "extraction_has_text", lambda e: False)
        units = [{"chapter_num": 0, "title": "Glossary", "start_page": 90,
                  "end_page": 99, "kind": "apparatus"}]
        monkeypatch.setattr(vc, "detect_chapters_vision", lambda *a, **k: units)
        out = vc.relocate_chapter_for_generation(
            "x.pdf", SimpleNamespace(total_pages=100, items=[]),
            {"title": "Unit 5: Photosynthesis", "start_page": 40}, object(),
        )
        # The only candidate is apparatus → filtered → no units → incomplete.
        assert out["status"] == "incomplete"
