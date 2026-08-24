"""TEXT RUNGS carry the born-digital fleet — the named regressions for the
bookmark removal (founder decision, 2026-08-24).

With the outline rung deleted, every book that used to ride a publisher
bookmark outline re-routes through the text detectors: running headers,
labelled markers, bare numbers, heading inference. Two live library shapes are
named in the decision and pinned here as synthetic text fixtures:

  * 'BUSINESS DYNAMICS' (5bd0d381, healthy-must-not-regress): 1,007 pages,
    text layer, 'PartⅠ'..'PartⅦ' banners with the U+2160 roman numeral GLUED
    straight onto the word — the letter-number shape that blinded _LABEL_RE
    once already — printed in running text above 21 'Chapter N' headings.
  * the Cambridge 37-chapter born-digital shape (a13a45e2's geometry): 37
    labelled chapter headings over ~700 pages.

Every fixture also PLANTS a junk bookmark outline on the extraction, because
the decision's contract is not "the text rungs work" but "the text rungs work
and the bookmarks are dead weight" — a regression that quietly re-reads
extraction.toc must fail these tests, not just the Sara file.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent1_ingestion.book_health import compute_book_health
from agent1_ingestion.extractor import DocItem, TOCItem
from agent1_ingestion.structurer import structure_book

# One academic page of text — the shape checks estimate lesson-parts from the
# extracted volume, so page ranges alone are not enough.
_PAGE_TEXT = (
    "Systems thinking asks us to look past the events we notice to the structure "
    "that keeps producing them. In this chapter we build a stock and flow model "
    "of a simple supply chain, run it, and compare what it does with what the "
    "managers in the case expected it to do. The gap between the two is telling. "
) * 9

_CHAPTER_TITLES = [
    "Learning in and about complex systems", "System dynamics in action",
    "The modeling process", "Structure and behavior of dynamic systems",
    "Causal loop diagrams", "Stocks and flows", "Dynamics of stocks and flows",
    "Closing the loop", "S-shaped growth", "Path dependence",
    "Delays", "Coflows and aging chains", "Modeling decision making",
    "Formulating nonlinear relationships", "Modeling human behavior",
    "The invisible hand", "Supply chains and the origin of oscillations",
    "The manufacturing supply chain", "The labor supply chain",
    "The commodity cycle", "Truth and beauty",
]

_JUNK_TOC = [TOCItem(level=1, title=f"BUSINESS DYNAMICS {i + 1}", page_num=i * 77)
             for i in range(13)]


def _body(total_pages: int) -> list[DocItem]:
    return [DocItem(item_type="paragraph", text=_PAGE_TEXT, page_num=p, level=0)
            for p in range(total_pages)]


def _extraction(items, total_pages, toc=None):
    return SimpleNamespace(
        toc=_JUNK_TOC if toc is None else toc, total_pages=total_pages,
        items=items, readability_score=0.9, extraction_backend="pymupdf",
        markdown="",
    )


def _headings(starts_titles, total_pages, level=1):
    out = [DocItem(item_type="title", text=t, page_num=p, level=level)
           for p, t in starts_titles]
    out += _body(total_pages)
    out.sort(key=lambda i: (i.page_num, -i.level))
    return out


class TestBusinessDynamics:
    """PartⅠ-Ⅶ over Chapter 1-21, 1,007 pages — the U+2160 book."""

    _TOTAL = 1007

    def _items(self):
        # 'PartⅠ Perspective and Process' — numeral GLUED, exactly as the real
        # book's machine-set banners print it (no space before U+2160).
        parts = [(i * 144, f"Part{ch} Perspective and Process")
                 for i, ch in enumerate("ⅠⅡⅢⅣⅤⅥⅦ")]
        chapters = [(i * 48, f"Chapter {i + 1} {_CHAPTER_TITLES[i]}")
                    for i in range(21)]
        return _headings(parts + chapters, self._TOTAL)

    def test_the_chapters_win_the_altitude_fight_without_bookmarks(self):
        book = structure_book(
            book_id="5bd0d381", title="BUSINESS DYNAMICS", author="Sterman",
            isbn=None, extraction=_extraction(self._items(), self._TOTAL), images=[],
        )
        assert book.total_chapters == 21
        assert [c.title for c in book.chapters] == _CHAPTER_TITLES
        assert [c.start_page for c in book.chapters] == [i * 48 for i in range(21)]
        # …at chapter altitude, never the 7 Parts of 144 pages.
        assert max(c.end_page - c.start_page + 1 for c in book.chapters) < 120

    def test_the_result_is_healthy_and_ungated(self):
        ext = _extraction(self._items(), self._TOTAL)
        book = structure_book(book_id="5bd0d381", title="BUSINESS DYNAMICS",
                              author="Sterman", isbn=None, extraction=ext, images=[])
        h = compute_book_health(ext, [
            {"chapter_num": c.chapter_num, "title": c.title,
             "start_page": c.start_page, "end_page": c.end_page}
            for c in book.chapters])
        assert h["gate"] == "none"
        assert h["band"] in ("good", "excellent")
        assert h["facts"]["chapter_quality"]["suspect"] is False

    def test_part_banners_alone_yield_the_coarse_map_not_one_chapter(self):
        # A book that prints ONLY its Part banners: the 7-part map is the
        # wrong altitude (144-page units, ~24 parts each) and is demoted — but
        # the demotion keeps it aside, and with nothing finer and no client it
        # is restored: 7 over-large units still beat one whole-book chapter.
        parts_only = _headings(
            [(i * 144, f"Part{ch} Perspective and Process")
             for i, ch in enumerate("ⅠⅡⅢⅣⅤⅥⅦ")], self._TOTAL)
        book = structure_book(
            book_id="5bd0d381", title="BUSINESS DYNAMICS", author="Sterman",
            isbn=None, extraction=_extraction(parts_only, self._TOTAL), images=[],
        )
        assert book.total_chapters == 7

    def test_the_junk_bookmarks_are_dead_weight(self):
        # Same items, wildly different outlines — identical result. The toc
        # can no longer even TIE-BREAK a map.
        maps = []
        for toc in ([], _JUNK_TOC,
                    [TOCItem(level=1, title=f"{i:03d}-C606", page_num=i * 90)
                     for i in range(11)]):
            book = structure_book(
                book_id="5bd0d381", title="BUSINESS DYNAMICS", author="Sterman",
                isbn=None,
                extraction=_extraction(self._items(), self._TOTAL, toc=toc),
                images=[],
            )
            maps.append([(c.title, c.start_page, c.end_page) for c in book.chapters])
        assert maps[0] == maps[1] == maps[2]


class TestCambridge37:
    """The 37-chapter born-digital shape (a13a45e2's geometry, clean titles)."""

    _TOTAL = 740

    def _items(self):
        titles = [f"Chapter {i + 1} {_CHAPTER_TITLES[i % 21]} {chr(65 + i % 26)}"
                  for i in range(37)]
        return _headings([(4 + i * 19, t) for i, t in enumerate(titles)], self._TOTAL)

    def test_all_37_chapters_come_off_the_printed_headings(self):
        book = structure_book(
            book_id="a13a45e2", title="Cambridge International AS and A Level "
            "Business", author="Stimpson", isbn=None,
            extraction=_extraction(self._items(), self._TOTAL), images=[],
        )
        assert book.total_chapters == 37
        assert book.chapters[0].start_page == 4
        assert book.chapters[-1].end_page == self._TOTAL - 1
        h = compute_book_health(
            _extraction(self._items(), self._TOTAL),
            [{"chapter_num": c.chapter_num, "title": c.title,
              "start_page": c.start_page, "end_page": c.end_page}
             for c in book.chapters])
        assert h["gate"] == "none"
        assert h["facts"]["chapter_quality"]["suspect"] is False


class TestRunningHeaders:
    """Cambridge-style running headers — the highest-signal text rung, and the
    route the IGCSE ICT shape takes now that its outline is ignored."""

    def test_a_running_header_book_maps_off_its_headers(self):
        items = []
        unit_titles = ["Types and components of computer systems",
                       "Input and output devices", "Storage devices and media",
                       "Networks and the effects of using them",
                       "The effects of using IT", "ICT applications"]
        # A printed contents page mentioning every unit — 3+ distinct numbers
        # on one page, which the detector must drop from the signal.
        for n, t in enumerate(unit_titles, start=1):
            items.append(DocItem(item_type="paragraph", level=0, page_num=2,
                                 text=f"Unit {n}: {t}"))
        for n, t in enumerate(unit_titles, start=1):
            for k in range(30):
                items.append(DocItem(item_type="paragraph", level=0,
                                     page_num=4 + (n - 1) * 30 + k,
                                     text=f"Unit {n}: {t}"))
        items += _body(190)
        items.sort(key=lambda i: (i.page_num, -i.level))
        book = structure_book(
            book_id="b", title="IGCSE ICT", author="CUP", isbn=None,
            extraction=_extraction(items, 190), images=[],
        )
        assert book.total_chapters == 6
        assert [c.title for c in book.chapters] == unit_titles
        assert book.chapters[0].start_page == 4
