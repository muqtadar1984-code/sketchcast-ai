"""The topic harvest writes NAMES and never a sentence of a book.

The catalogue's founding rule (plan §1.1): textbooks are used only to harvest
topic names — no page spans, no book text. The harvest necessarily reads the
whole PDF to find its headings, so the rule is enforced at the one exit,
``catalogue.harvest.is_heading``, and these tests throw book prose at it
directly and then run the whole job against a fake Supabase that records every
write.

Everything here CALLS things (tests/test_worker_entrypoint_runs.py explains
why a source-substring test is worth nothing here). No network, no model, no
live Supabase; the PDF in the end-to-end test is built in the test with
PyMuPDF and structured by the real extractor.
"""

from __future__ import annotations

import pytest

from catalogue import harvest
from catalogue.harvest import (
    build_candidates, clean_heading, harvest_book, headings_from_book_row,
    headings_from_structured, insert_candidates, is_heading, run_harvest_job,
)
from catalogue.key import canonical_key
from tests.catalogue_fakes import DuplicateKey, FakeSB

BOOK = "book-1"
JOB = "job-th"

SENTENCE_200 = (
    "The cell is the basic structural and functional unit of every living organism, "
    "and it was first described by Robert Hooke in 1665 when he looked at a thin slice "
    "of cork under his microscope and saw tiny boxes."
)
assert len(SENTENCE_200) >= 200


def _book(chapters=None, storage_path="u1/books/b1.pdf", **extra):
    return {"id": BOOK, "title": "Science 7", "storage_path": storage_path,
            "chapters": chapters if chapters is not None else [
                {"num": 1, "title": "Cells", "start_page": 0, "end_page": 3},
                {"num": 2, "title": "Acids, Bases & Salts", "start_page": 4, "end_page": 9},
            ], **extra}


def _job(book_id=BOOK, **extra):
    return {"id": JOB, "type": "topic_harvest", "book_id": book_id, "generation_id": None,
            "status": "processing", **extra}


def _sb(book=None, jobs=None, aliases=(), candidates=()):
    sb = FakeSB()
    sb.tables["books"] = [book] if book else []
    sb.tables["jobs"] = jobs if jobs is not None else [_job()]
    sb.tables["generations"] = [{"id": "gen-other", "status": "done"}]
    sb.tables["topic_aliases"] = [dict(a) for a in aliases]
    sb.tables["topic_candidates"] = [dict(c) for c in candidates]
    return sb


def _titles(sb):
    return [r["raw_title"] for r in sb.tables["topic_candidates"]]


# ── the gate ────────────────────────────────────────────────────────────


class TestIsHeading:
    @pytest.mark.parametrize("text", [
        "Cell", "Cells", "1.2 Cells.", "Chapter 1  Cells", "What is matter?",
        "Acids, Bases & Salts", "Light — Reflection and Refraction", "Newton's Laws",
        "7Bs.01", "CO2", "Activity 1.2", "1.1 The cell membrane", "Énergie",
    ])
    def test_headings_pass(self, text):
        assert is_heading(text) is True

    @pytest.mark.parametrize("text", [
        SENTENCE_200,                                   # a 200-char sentence
        "Fig. 3 shows the cell",                        # a caption is prose
        "The cell is the basic unit of life. It was discovered in 1665.",
        "What is matter? Everything around us",         # a question then prose
        "Look! A cell",                                 # exclamation then prose
        "e.g. cells",                                   # abbreviation then a word
        "x" * 121,                                      # longer than 120 chars
        "", "  ", None,                                 # nothing
        "A", "1", "12", "1.1", "3 / 4", "§ 2.1",        # fewer than two letters
        "Page 12", "page 12", "p. 12", "pp. 12-14", "pg 7",  # folios
    ])
    def test_prose_and_noise_are_dropped(self, text):
        assert is_heading(text) is False

    def test_the_boundary_is_exactly_120(self):
        assert is_heading("a" * 120) is True
        assert is_heading("a" * 121) is False

    def test_a_trailing_period_is_not_a_sentence(self):
        """A heading may end in a full stop; prose has words AFTER it."""
        assert is_heading("1.2 Cells.") is True
        assert is_heading("1.2 Cells. They are small") is False

    def test_whitespace_is_collapsed_first(self):
        assert clean_heading("  Acids,\n  Bases   &\tSalts ") == "Acids, Bases & Salts"
        assert is_heading("Cells\n") is True


# ── the pure pipeline ───────────────────────────────────────────────────


class TestBuildCandidates:
    def test_gate_key_dedupe_in_order(self):
        out = build_candidates(["Cells", SENTENCE_200, "The Cell", "cell", "Atoms", "Fig. 3 shows the cell"])
        assert out == [{"raw_title": "Cells", "normalized": "cell"},
                       {"raw_title": "Atoms", "normalized": "atom"}]

    def test_the_first_spelling_of_a_key_wins(self):
        out = build_candidates(["The Cell", "Cells"])
        assert out == [{"raw_title": "The Cell", "normalized": "cell"}]

    def test_a_heading_with_no_key_material_is_dropped(self):
        assert build_candidates(["The", "An", "الخلية"]) == []

    def test_the_cap_trims_the_tail(self):
        out = build_candidates([f"Topic {i}" for i in range(50)], limit=10)
        assert len(out) == 10 and out[0]["raw_title"] == "Topic 0"

    def test_raw_title_is_at_most_120(self):
        for c in build_candidates(["b" * 120]):
            assert len(c["raw_title"]) <= 120


class TestHeadingsFromStructured:
    def test_titles_sections_and_subsections_but_not_the_placeholder(self):
        structured = {"chapters": [
            {"title": "Cells", "sections": [
                {"section_title": "1.1 The cell membrane", "section_type": "heading", "content": SENTENCE_200,
                 "subsections": [{"section_title": "Diffusion", "content": SENTENCE_200}]},
                {"section_title": "Content", "section_type": "body", "content": SENTENCE_200},
            ]},
            {"title": "Acids, Bases & Salts", "sections": []},
        ]}
        assert headings_from_structured(structured) == [
            "Cells", "Acids, Bases & Salts", "1.1 The cell membrane", "Diffusion"]

    def test_section_content_never_appears(self):
        structured = {"chapters": [{"title": "Cells", "sections": [
            {"section_title": "Membranes", "section_type": "heading", "content": SENTENCE_200}]}]}
        assert SENTENCE_200 not in " ".join(headings_from_structured(structured))

    def test_book_row_titles(self):
        assert headings_from_book_row(_book()) == ["Cells", "Acids, Bases & Salts"]
        assert headings_from_book_row({"chapters": None}) == []


# ── the job, end to end against the fake ────────────────────────────────


PDF_HEADINGS = ["Cells", "1.1 The cell membrane", "1.2 Diffusion.", SENTENCE_200,
                "Fig. 3 shows the cell", "Acids, Bases & Salts", "2.1 Indicators", "Page 12"]


@pytest.fixture
def no_pdf(monkeypatch):
    """The download and structuring stand in: the PDF 'yields' PDF_HEADINGS."""
    monkeypatch.setattr(harvest.db, "download_book",
                        lambda sb, path, dest: (sb.downloads.append(path), dest)[1])
    monkeypatch.setattr(harvest, "_headings_from_pdf", lambda pdf_path, book: list(PDF_HEADINGS))


def test_the_job_writes_only_headings_and_finishes_done(no_pdf):
    sb = _sb(_book())
    summary = run_harvest_job(sb, _job())

    titles = _titles(sb)
    assert titles == ["Cells", "Acids, Bases & Salts", "1.1 The cell membrane", "1.2 Diffusion.",
                      "2.1 Indicators"]
    assert SENTENCE_200 not in titles and "Fig. 3 shows the cell" not in titles
    for row in sb.tables["topic_candidates"]:
        assert row["source_kind"] == "book" and row["book_id"] == BOOK
        assert row["normalized"] == canonical_key(row["raw_title"])
        assert row["suggested_topic_id"] is None
        assert len(row["raw_title"]) <= 120
        assert "node_id" not in row or row["node_id"] is None

    job = sb.tables["jobs"][0]
    assert job["status"] == "done" and job["progress"] == 100 and job["error"] is None
    assert job["stage"]["step"] == "done"
    assert job["stage"]["inserted"] == 5 and job["stage"]["candidates"] == 5
    assert job["stage"]["source"] == "pdf+chapters"
    assert summary == job["stage"]
    assert sb.downloads == ["u1/books/b1.pdf"]


def test_no_written_string_is_a_sentence(no_pdf):
    """The guarantee, asked of what was WRITTEN rather than of the gate."""
    sb = _sb(_book())
    run_harvest_job(sb, _job())
    for op, table, payload, *_ in sb.writes("topic_candidates"):
        for row in (payload if isinstance(payload, list) else [payload]):
            assert is_heading(row["raw_title"]), row


def test_the_generation_table_is_never_written(no_pdf):
    sb = _sb(_book())
    run_harvest_job(sb, _job())
    assert sb.writes("generations") == []
    # Even a job row carrying a generation_id (never filed that way, but the
    # observer rule says: never) leaves that generation alone.
    sb = _sb(_book())
    sb.tables["jobs"][0]["generation_id"] = "gen-other"
    run_harvest_job(sb, _job(generation_id="gen-other"))
    assert sb.writes("generations") == [] and sb.tables["generations"][0]["status"] == "done"


def test_alias_suggestion(no_pdf):
    sb = _sb(_book(), aliases=[{"topic_id": "topic-cell", "alias": "The Cell", "normalized": "cell"},
                               {"topic_id": "topic-diff", "alias": "Diffusion", "normalized": "1_2_diffusion"}])
    run_harvest_job(sb, _job())
    by_key = {r["normalized"]: r["suggested_topic_id"] for r in sb.tables["topic_candidates"]}
    assert by_key["cell"] == "topic-cell"
    assert by_key["1_2_diffusion"] == "topic-diff"
    assert by_key["acid_base_and_salt"] is None
    assert sb.tables["jobs"][0]["stage"]["suggested"] == 2


def test_a_second_harvest_of_the_same_book_inserts_nothing(no_pdf):
    sb = _sb(_book())
    run_harvest_job(sb, _job())
    before = sb.snapshot()["topic_candidates"]
    n_writes = len(sb.writes("topic_candidates"))

    run_harvest_job(sb, _job())
    assert sb.tables["topic_candidates"] == before
    assert len(sb.writes("topic_candidates")) == n_writes, "nothing was inserted the second time"
    stage = sb.tables["jobs"][0]["stage"]
    assert stage["inserted"] == 0 and stage["existing"] == 5 and stage["candidates"] == 5


def test_a_dismissed_candidate_is_not_reopened(no_pdf):
    """A curator dismissed 'Cells'; a re-harvest must not file it again."""
    sb = _sb(_book(), candidates=[{"source_kind": "book", "book_id": BOOK, "raw_title": "Cells",
                                   "normalized": "cell", "status": "dismissed"}])
    run_harvest_job(sb, _job())
    cells = [r for r in sb.tables["topic_candidates"] if r["normalized"] == "cell"]
    assert len(cells) == 1 and cells[0]["status"] == "dismissed"


def test_another_book_with_the_same_heading_still_gets_its_own_row(no_pdf):
    sb = _sb(_book(), candidates=[{"source_kind": "book", "book_id": "book-other", "raw_title": "Cells",
                                   "normalized": "cell"}])
    run_harvest_job(sb, _job())
    assert sorted(r["book_id"] for r in sb.tables["topic_candidates"] if r["normalized"] == "cell") == \
        [BOOK, "book-other"]


def test_a_racing_harvest_is_absorbed_row_by_row():
    """Two workers harvesting one book: the chunk insert hits the unique
    index; the fallback inserts the rows that are new and skips the rest."""
    sb = _sb(_book(), candidates=[{"source_kind": "book", "book_id": BOOK, "raw_title": "Cells",
                                   "normalized": "cell"}])
    rows = [{"source_kind": "book", "book_id": BOOK, "raw_title": t, "normalized": canonical_key(t),
             "suggested_topic_id": None} for t in ["Cells", "Atoms", "Forces"]]
    assert insert_candidates(sb, rows) == 2
    assert sorted(_titles(sb)) == ["Atoms", "Cells", "Forces"]


def test_a_non_duplicate_insert_error_is_not_swallowed(monkeypatch):
    sb = _sb(_book())

    class Boom(Exception):
        pass

    def exploding_table(name):
        q = FakeSB.table(sb, name)
        if name == "topic_candidates":
            q.execute = lambda: (_ for _ in ()).throw(Boom("permission denied for table"))
        return q

    monkeypatch.setattr(sb, "table", exploding_table)
    with pytest.raises(Boom):
        insert_candidates(sb, [{"source_kind": "book", "book_id": BOOK, "raw_title": "Cells",
                                "normalized": "cell"}])


def test_duplicate_detection_reads_the_code_and_the_message():
    assert harvest._is_duplicate_error(DuplicateKey("topic_candidates", ("k",)))
    assert harvest._is_duplicate_error(RuntimeError("duplicate key value violates unique constraint"))
    assert not harvest._is_duplicate_error(RuntimeError("permission denied"))


# ── failure paths ───────────────────────────────────────────────────────


def test_a_missing_book_finishes_the_job_with_error(no_pdf):
    sb = _sb(book=None)
    assert run_harvest_job(sb, _job()) is None
    job = sb.tables["jobs"][0]
    assert job["status"] == "error" and "not found" in job["error"] and job["progress"] == 0
    assert sb.tables["topic_candidates"] == [] and sb.writes("generations") == []


def test_a_job_without_a_book_id_finishes_with_error(no_pdf):
    sb = _sb(_book())
    run_harvest_job(sb, _job(book_id=None))
    assert sb.tables["jobs"][0]["status"] == "error"
    assert "book_id" in sb.tables["jobs"][0]["error"]


def test_removed_content_is_refused(no_pdf):
    sb = _sb(_book(removed_at="2026-09-01T00:00:00Z"))
    run_harvest_job(sb, _job())
    assert sb.tables["jobs"][0]["status"] == "error" and "removed" in sb.tables["jobs"][0]["error"]
    assert sb.tables["topic_candidates"] == []


def test_a_failing_download_still_harvests_the_stored_titles(monkeypatch):
    """The PDF is a bonus; books.chapters is the indexed truth."""
    def dead_download(sb, path, dest):
        raise RuntimeError("StreamReset")
    monkeypatch.setattr(harvest.db, "download_book", dead_download)
    sb = _sb(_book())
    run_harvest_job(sb, _job())
    assert _titles(sb) == ["Cells", "Acids, Bases & Salts"]
    stage = sb.tables["jobs"][0]["stage"]
    assert sb.tables["jobs"][0]["status"] == "done"
    assert stage["source"] == "chapters_only" and "StreamReset" in stage["pdf_error"]


def test_a_book_without_a_storage_path_harvests_titles_only(no_pdf):
    sb = _sb(_book(storage_path=None))
    run_harvest_job(sb, _job())
    assert _titles(sb) == ["Cells", "Acids, Bases & Salts"]
    assert sb.tables["jobs"][0]["stage"]["pdf_error"] == "book has no storage_path"
    assert sb.downloads == []


def test_a_database_failure_after_the_read_is_recorded(monkeypatch, no_pdf):
    sb = _sb(_book())
    monkeypatch.setattr(harvest, "lookup_alias_topics",
                        lambda sb_, keys: (_ for _ in ()).throw(RuntimeError("postgrest 503")))
    assert run_harvest_job(sb, _job()) is None
    assert sb.tables["jobs"][0]["status"] == "error" and "503" in sb.tables["jobs"][0]["error"]
    assert sb.tables["topic_candidates"] == []


# ── a real PDF through the real extractor ───────────────────────────────


BODY = [
    "The cell is the basic unit of life. Every living thing is made of cells.",
    "Robert Hooke first saw cells in 1665 when he looked at cork. He called them cells because they",
    "reminded him of the small rooms in a monastery. Fig. 1 shows his drawing of the cork.",
    "Acids taste sour and turn blue litmus red. Bases feel soapy and turn red litmus blue.",
]


def _make_pdf(path):
    """Two chapters with a big title, medium section headings and small body
    prose — the font-size shape the PyMuPDF backend classifies on."""
    import fitz

    doc = fitz.open()
    chapters = [
        ("Chapter 1  Cells", ["1.1 The cell membrane", "1.2 Diffusion"]),
        ("Chapter 2  Acids, Bases and Salts", ["2.1 Indicators", "2.2 Neutralisation"]),
    ]
    for title, sections in chapters:
        for s in sections:
            page = doc.new_page()
            page.insert_text((72, 80), title, fontsize=22)
            page.insert_text((72, 130), s, fontsize=16)
            y = 170
            for line in BODY * 3:
                page.insert_text((72, y), line, fontsize=10)
                y += 16
    doc.save(str(path))
    doc.close()


def test_a_real_pdf_yields_headings_and_no_prose(tmp_path, monkeypatch):
    pdf = tmp_path / "book.pdf"
    _make_pdf(pdf)
    monkeypatch.setenv("DOCLING_BACKEND", "pymupdf")
    from agent1_ingestion import extractor
    # Cache under tmp so the run never touches storage/docling_cache.
    real_extract = extractor.extract_pdf
    monkeypatch.setattr(extractor, "extract_pdf",
                        lambda p, cache_dir=None: real_extract(p, cache_dir=tmp_path / "cache"))

    sb = _sb(_book(chapters=[
        {"num": 1, "title": "Cells", "start_page": 0, "end_page": 1},
        {"num": 2, "title": "Acids, Bases and Salts", "start_page": 2, "end_page": 3},
    ]))
    sb.files[("uploads", "u1/books/b1.pdf")] = pdf.read_bytes()

    summary = harvest_book(sb, JOB, sb.tables["books"][0])

    titles = _titles(sb)
    keys = {r["normalized"] for r in sb.tables["topic_candidates"]}
    assert "cell" in keys and "acid_base_and_salt" in keys
    # Only a LEADING article is dropped, so "1.1 The cell membrane" keeps its "the".
    assert {"1_1_the_cell_membrane", "1_2_diffusion", "2_1_indicator", "2_2_neutralisation"} <= keys, keys
    prose = " ".join(BODY).lower()
    for t in titles:
        assert is_heading(t), t
        assert t.lower() not in prose or len(t) < 25, f"a body line leaked: {t!r}"
        assert "hooke" not in t.lower() and "litmus" not in t.lower(), t
    assert summary["source"] == "pdf+chapters" and summary["inserted"] == len(titles) > 0
    assert sb.downloads == [("uploads", "u1/books/b1.pdf")]
