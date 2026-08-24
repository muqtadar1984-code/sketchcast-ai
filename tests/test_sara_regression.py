"""SARA JUNAIDI'S BOOK — the named end-to-end regression (2026-08-23).

Ground truth extracted ONCE from the real 132MB PDF
(899612641-Science-Learner-s-Book-8.pdf) into
tests/fixtures/sara_science_learners_book_8.json: 341 pages, ZERO text-layer
characters on every page, KamiHQ producer, and 13 embedded bookmarks that are
pure junk — '1'@p1 plus 'LEANERS BOOK 8 1..12', the book's own misspelled
cover title with a serial, typed by whoever scanned it.

What production did with it: indexed 13 chapters on those bookmarks, glued
real section headings onto the wrong boundaries at heal time ('2.4 Paper
chromatography' over 66 pages containing a force diagram), orphaned pages
26-61 (36 pages no kit can ever be generated from), estimated an 11- and a
12-part chapter (~78 credits behind "Generate all"), and blessed the lot at
health 82 "good", problems [], gate "none", unmapped_pages 0, with the note
"Scanned book — read by AI vision (works well…)" beside the nonsense list.

The contract pinned here, per the founder ("with no feedback being received we
have to ensure the guardrails hold tight"): feeding this book through the
pipeline must yield either a SANE chapter list (the vision rung finally
reached) or a LOW-CONFIDENCE GATED result — never 13 silent "good" chapters.

2026-08-24, the founder went further: PDF bookmarks are GONE as a structure
source entirely (see the structurer's block comment). This book's 13 junk
bookmarks can no longer build a map AT ALL — the first test below pins the
rung's absence — and the no-client exit changed shape accordingly: a zero-text
scan with no vision collapses to ONE whole-book chapter, gated by health's
one-unit arm, instead of riding typed metadata. The outcome validator stays
load-bearing for STORED pre-fix rows, which health judges forever. The
clean-book regression moved with the rung: a born-digital book must now index
identically off its PRINTED headings (tests/test_text_rungs.py pins the named
fleet shapes).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent1_ingestion import structurer
from agent1_ingestion.book_health import compute_book_health
from agent1_ingestion.extractor import TOCItem
from agent1_ingestion.structurer import _chapters_plausible, structure_book

FIXTURE = Path(__file__).parent / "fixtures" / "sara_science_learners_book_8.json"


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _sara_extraction():
    fx = _fixture()
    toc = [TOCItem(level=t["level"], title=t["title"], page_num=t["page_1based"] - 1)
           for t in fx["toc"]]
    # per_page_chars is all zeros — the zero-text-layer proof — so items is [].
    assert sum(fx["per_page_chars"]) == 0
    return SimpleNamespace(
        toc=toc, total_pages=fx["total_pages"], items=[],
        readability_score=0.0, extraction_backend="pymupdf", markdown="",
    )


def _health_defs(book):
    """books.chapters as index_book would store them, from a StructuredBook."""
    return [{"chapter_num": c.chapter_num, "title": c.title,
             "start_page": c.start_page, "end_page": c.end_page}
            for c in book.chapters]


class TestTheCascade:
    def test_the_bookmark_rung_is_gone_not_disabled(self):
        # Founder decision 2026-08-24: deleted, not fenced — a disabled rung is
        # one refactor away from being re-enabled by accident. Pinning the
        # symbols' ABSENCE is what makes an accidental restoration fail loudly.
        for name in ("_toc_is_usable", "_build_chapters_from_toc",
                     "_pick_toc_depth", "_looks_like_file_bookmark",
                     "_depth_is_sections"):
            assert not hasattr(structurer, name), f"{name} came back"

    def test_the_junk_bookmarks_cannot_build_a_map_any_more(self):
        # The extraction still CARRIES the 13 junk bookmarks (extractor.py
        # keeps extracting the outline — health's thin-fragment gate and
        # support bundles read its presence) — but nothing may seed chapters
        # from them. With no text layer and no client, the only map left is
        # the whole-document fallback: ONE honest chapter, never 13 silent
        # 'good' ones.
        ext = _sara_extraction()
        assert len(ext.toc) == 13  # the junk is present and ignored
        book = structure_book(
            book_id="e0459f87", title="Sciencew Book", author="?", isbn=None,
            extraction=ext, images=[], pdf_path="x.pdf", client=None,
        )
        assert book.total_chapters == 1
        assert book.chapters[0].start_page == 0
        assert book.chapters[0].end_page == ext.total_pages - 1

    def test_the_outcome_check_still_refuses_the_stored_junk_shape(self):
        # The validator outlives the rung: pre-fix books carry these 13
        # boundaries in STORED rows, and health re-judges stored lists
        # forever. The incident's exact map must keep failing plausibility
        # whichever detector (or database row) offers it.
        fx = _fixture()
        starts = [t["page_1based"] - 1 for t in fx["toc"]]
        junk = [{"chapter_num": i, "title": t["title"], "start_page": starts[i],
                 "end_page": (starts[i + 1] - 1) if i + 1 < len(starts)
                 else fx["total_pages"] - 1}
                for i, t in enumerate(fx["toc"])]
        assert len(junk) == 13  # the exact incident boundaries
        assert _chapters_plausible(junk, fx["total_pages"], {}) is False

    def test_the_vision_rung_is_finally_reached(self, monkeypatch):
        # THE fence bug: 13 junk chapters at 100% coverage kept
        # `len(chapter_defs) <= 1 or poor_coverage` permanently shut, so the
        # one detector that READS a scanned book's pages never ran. Demoting
        # the junk map must open it.
        from agent1_ingestion import vision_chapters

        sane = [{"chapter_num": i, "title": t, "start_page": s, "end_page": e}
                for i, (t, s, e) in enumerate([
                    ("1 Respiration", 6, 21), ("2 Properties of materials", 22, 61),
                    ("3 Forces and motion", 62, 89), ("4 Ecosystems", 90, 119),
                ])]
        calls: list[str] = []

        def _fake_vision(pdf_path, total_pages, client):
            calls.append("vision")
            return sane

        monkeypatch.setattr(vision_chapters, "detect_chapters_vision", _fake_vision)
        book = structure_book(
            book_id="e0459f87", title="Sciencew Book", author="?", isbn=None,
            extraction=_sara_extraction(), images=[], pdf_path="x.pdf", client=object(),
        )
        assert calls == ["vision"]
        assert [c.title for c in book.chapters] == [c["title"] for c in sane]
        # …and the sane list is NOT gated: the rescue is a clean exit.
        h = compute_book_health(_sara_extraction(), _health_defs(book))
        assert h["facts"]["chapter_quality"]["suspect"] is False

    def test_without_vision_the_result_is_gated_never_silently_good(self):
        # No client (the heuristics-only path). With the bookmark rung gone
        # this zero-text scan collapses to ONE whole-book chapter — and a
        # 341-page book as one unit is a detection failure worth a dialog:
        # health's one-unit gate arm (>= _ONE_UNIT_GATE_PAGES) must hold the
        # line the junk bookmarks used to slip. This replaces the exact
        # configuration that produced 82/good/problems[]/gate none.
        ext = _sara_extraction()
        book = structure_book(
            book_id="e0459f87", title="Sciencew Book", author="?", isbn=None,
            extraction=ext, images=[], pdf_path="x.pdf", client=None,
        )
        assert book.total_chapters == 1
        h = compute_book_health(ext, _health_defs(book))
        assert h["gate"] == "confirm"                      # was "none"
        assert h["band"] not in ("good", "excellent")      # was "good" (82)
        assert h["problems"] != []                         # was []
        assert h["note"] is None                           # was "…works well…"
        assert any("one unit" in p.lower() for p in h["problems"])

    def test_the_stored_incident_row_reindexed_is_caught_too(self):
        # Pre-fix books re-index through known_chapters=None only on upload;
        # their STORED row meanwhile flows straight into compute_book_health
        # style consumers. The stored list — healed titles, junk boundaries,
        # the 26-61 hole — must gate and report its hole.
        fx = _fixture()
        bounds = [(0, 5), (6, 7), (8, 21), (22, 25), (62, 127), (128, 165),
                  (166, 177), (178, 193), (194, 197), (198, 221), (222, 240),
                  (241, 325), (326, 340)]
        stored = [{"chapter_num": i, "title": t, "start_page": s, "end_page": e}
                  for i, (t, (s, e)) in enumerate(zip(fx["stored_incident_titles"], bounds))]
        h = compute_book_health(_sara_extraction(), stored)
        assert h["facts"]["unmapped_pages"] == 36          # was 0, WRONG
        assert h["gate"] == "confirm"
        assert h["band"] not in ("good", "excellent")


class TestNoRegression:
    """Requirement 6, re-seated for the bookmark-free world: a clean
    born-digital book must index EXACTLY the same map off its PRINTED unit
    headings — the text rungs the publisher-outline books re-route through
    now. The whole expected map is pinned byte-for-byte — any drift in
    boundaries, titles, numbering or count fails loudly. (A junk outline is
    planted on the same extraction to pin that bookmarks cannot even
    influence the result any more.)"""

    _UNIT_TITLES = ["Plants", "Rocks and soils", "States of matter", "Sound",
                    "Light and shadows", "Magnets and springs", "Habitats",
                    "Keeping healthy", "Earth and beyond"]

    def _clean_extraction(self):
        # Cambridge Primary Science Y7's shape: 9 units / 344 pages, printed
        # "Unit N <title>" headings — the corpus's largest legitimate median.
        # The junk toc is deliberate: it must be dead weight.
        junk_toc = [TOCItem(level=1, title=f"LEANERS BOOK 7 {i + 1}", page_num=i * 26)
                    for i in range(13)]
        text = ("Cells are the basic units of every living organism and in this "
                "chapter we look at their structure. ") * 8
        items = [SimpleNamespace(item_type="title", level=1, page_num=i * 38,
                                 text=f"Unit {i + 1} {t}")
                 for i, t in enumerate(self._UNIT_TITLES)]
        items += [SimpleNamespace(item_type="paragraph", text=text, page_num=p, level=0)
                  for p in range(344)]
        items.sort(key=lambda i: (i.page_num, -i.level))
        return SimpleNamespace(toc=junk_toc, total_pages=344, items=items,
                               readability_score=0.9, extraction_backend="pymupdf",
                               markdown="")

    def test_a_clean_book_indexes_byte_identically_off_its_printed_headings(self):
        book = structure_book(
            book_id="b", title="Science Y7", author="CUP", isbn=None,
            extraction=self._clean_extraction(), images=[],
        )
        got = [(c.chapter_num, c.title, c.start_page, c.end_page) for c in book.chapters]
        assert got == [
            (i, t, i * 38, (i * 38 + 37) if i < 8 else 343)
            for i, t in enumerate(self._UNIT_TITLES)
        ]

    def test_a_clean_book_never_pays_for_a_detection_pass(self):
        # The demotion path must not start billing good books: with a client
        # present, the vision/LLM rung stays shut for sound printed headings.
        from agent1_ingestion import vision_chapters

        def _boom(*_a, **_k):
            raise AssertionError("paid detection pass on a clean book")

        originals = (vision_chapters.detect_chapters_from_text_llm,
                     vision_chapters.detect_chapters_vision)
        vision_chapters.detect_chapters_from_text_llm = _boom
        vision_chapters.detect_chapters_vision = _boom
        try:
            book = structure_book(
                book_id="b", title="Science Y7", author="CUP", isbn=None,
                extraction=self._clean_extraction(), images=[], pdf_path="x.pdf",
                client=object(),
            )
        finally:
            (vision_chapters.detect_chapters_from_text_llm,
             vision_chapters.detect_chapters_vision) = originals
        assert book.total_chapters == 9

    def test_a_clean_book_health_is_untouched(self):
        ext = self._clean_extraction()
        book = structure_book(book_id="b", title="Science Y7", author="CUP",
                              isbn=None, extraction=ext, images=[])
        h = compute_book_health(ext, [
            {"chapter_num": c.chapter_num, "title": c.title,
             "start_page": c.start_page, "end_page": c.end_page}
            for c in book.chapters])
        assert h["gate"] == "none" and h["problems"] == []
        assert h["band"] == "excellent"
        assert h["facts"]["unmapped_pages"] == 0


class TestFixtureIsGroundTruth:
    def test_the_fixture_matches_the_incident(self):
        fx = _fixture()
        assert fx["total_pages"] == 341
        assert len(fx["toc"]) == 13
        assert fx["toc"][1]["title"] == "LEANERS BOOK 8 1"
        assert "KamiHQ" in fx["producer"]
        assert len(fx["per_page_chars"]) == 341 and sum(fx["per_page_chars"]) == 0
        assert len(fx["stored_incident_titles"]) == 13
