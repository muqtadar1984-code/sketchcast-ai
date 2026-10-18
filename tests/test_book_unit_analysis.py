"""One analysis per book chapter part, shared by every piece of a kit
(catalogue/analysis_cache book units, migration 0144).

Measured before it, on the Bedrock cut-over sample kit (2026-10-18, founder's
book ch1 part 1, Arabic): the worksheet, activity, case study, lesson plan
and deck each ran the agent2 analysis of the SAME part — $0.43–0.54 each,
all claimed within one second. A part job never reads chapter_grounding
(it describes the whole chapter and is the tutor's fence), so nothing was
ever shared. These tests pin the book key and the protocol on it; the
catalogue suite (tests/test_catalogue_kit.py::TestSharedAnalysis) pins the
worker wiring both kinds of unit now share.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from catalogue import analysis_cache as ac
from tests.catalogue_fakes import FakeSB

BOOK = "8fce6e4c-a369-4b61-b677-2dbf9c3089f3"
ANALYSIS = {"concepts": {"concepts": [{"concept_id": "c1", "name": "particles"}]},
            "episodes": {"episodes": [{"episode_num": 1, "title": "Materials"}]}, "language": "ar"}
CHUNK = {"text": "Solids, liquids and gases are made of particles.", "words": 8, "section_titles": ["States"]}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("BOOK_SHARED_ANALYSIS", raising=False)
    monkeypatch.setattr(ac, "analyzer_version", lambda: 2)


def _claim(sb, gen, *, part=1, lang="ar", chunks=(CHUNK,), waited=0.0, chapter=None):
    unit = ac.book_unit(BOOK, 1, part, lang)
    h = ac.book_source_hash(language=lang, level="middle_school", chunks=list(chunks), chapter=chapter)
    return unit, ac.claim_unit(sb, unit, generation_id=gen, language=lang, source_hash=h, waited_seconds=waited)


def _rows(sb):
    return sb.tables.get(ac.BOOK_TABLE, [])


# ── the key ─────────────────────────────────────────────────────────────

def test_the_key_names_book_chapter_part_and_language():
    assert ac.book_unit(BOOK, 1, 1, "ar").key == f"{BOOK}:1:1:ar"
    assert ac.book_unit(BOOK, 1, None, "en").key == f"{BOOK}:1:all:en", "a whole-chapter kit has its own row"
    u = ac.book_unit(BOOK, "3", 2, "ar")
    assert u.insert_fields == {"book_id": BOOK, "chapter_num": 3, "part": 2}
    assert u.table == "book_unit_analyses" and u.key_col == "unit_key" and u.revision_col is None


def test_figures_do_not_move_a_whole_chapter_hash_but_text_does():
    """A whole-chapter job hashes the chapter; its figure list can carry
    per-job local paths, which would give every sibling a different key."""
    ch = {"chapter_num": 1, "title": "Materials", "sections": [{"section_title": "S", "content": "text"}],
          "images": [{"path": "/tmp/job-a/fig1.png"}]}
    other_job = {**ch, "images": [{"path": "/tmp/job-b/fig1.png"}]}
    edited = {**ch, "sections": [{"section_title": "S", "content": "text, re-indexed"}]}
    h = lambda c: ac.book_source_hash(language="en", level="x", chunks=[], chapter=c)  # noqa: E731
    assert h(ch) == h(other_job)
    assert h(ch) != h(edited)


def test_a_part_hash_is_its_chunk_text():
    h = lambda c: ac.book_source_hash(language="ar", level="x", chunks=[c], chapter=None)  # noqa: E731
    assert h(CHUNK) == h(dict(CHUNK))
    assert h(CHUNK) != h({**CHUNK, "text": CHUNK["text"] + " More."})


# ── the protocol on a book unit ─────────────────────────────────────────

def test_a_kit_of_five_pieces_claimed_together_analyses_once():
    """The cut-over kit's shape: one holder, four siblings that wait, then
    every one of them reuses the stored dict."""
    sb = FakeSB()
    unit, first = _claim(sb, "gen-worksheet")
    assert (first.action, first.held) == ("analyse", True)
    (row,) = _rows(sb)
    assert (row["unit_key"], row["book_id"], row["chapter_num"], row["part"], row["status"]) == \
        (f"{BOOK}:1:1:ar", BOOK, 1, 1, "analysing")

    for sibling in ("gen-activity", "gen-case", "gen-plan", "gen-deck"):
        _, v = _claim(sb, sibling)
        assert (v.action, v.holder) == ("wait", "gen-worksheet"), sibling

    assert ac.store_unit(sb, unit, generation_id="gen-worksheet", analysis=ANALYSIS) is True
    for sibling in ("gen-activity", "gen-case", "gen-plan", "gen-deck"):
        _, v = _claim(sb, sibling)
        assert v.action == "reuse" and v.analysis == ANALYSIS, sibling
    assert len(_rows(sb)) == 1


def test_languages_and_parts_never_share():
    sb = FakeSB()
    unit, _ = _claim(sb, "gen-ar")
    ac.store_unit(sb, unit, generation_id="gen-ar", analysis=ANALYSIS)
    assert _claim(sb, "gen-en", lang="en")[1].action == "analyse", "an Arabic analysis never grounds English"
    assert _claim(sb, "gen-p2", part=2)[1].action == "analyse", "part 2 is a different unit"
    assert _claim(sb, "gen-all", part=None, chunks=(), chapter={"title": "Materials"})[1].action == "analyse"
    assert len(_rows(sb)) == 4


def test_a_re_indexed_part_is_re_analysed_and_overwrites():
    sb = FakeSB()
    unit, _ = _claim(sb, "gen-old")
    ac.store_unit(sb, unit, generation_id="gen-old", analysis=ANALYSIS)
    _, v = _claim(sb, "gen-new", chunks=({**CHUNK, "text": "New text after a relocation."},))
    assert (v.action, v.held) == ("analyse", True)
    (row,) = _rows(sb)
    assert (row["holder_generation_id"], row["status"]) == ("gen-new", "analysing")


def test_a_failed_holder_releases_so_a_sibling_analyses_at_once():
    sb = FakeSB()
    unit, _ = _claim(sb, "gen-a")
    ac.release_unit(sb, unit, generation_id="gen-a")
    assert _rows(sb) == []
    _, v = _claim(sb, "gen-b")
    assert (v.action, v.held) == ("analyse", True)


def test_a_stale_lease_is_taken_over():
    sb = FakeSB()
    _claim(sb, "gen-crashed")
    _rows(sb)[0]["lease_started_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _, v = _claim(sb, "gen-b")
    assert (v.action, v.held) == ("analyse", True) and "stale lease" in v.reason


def test_a_sibling_that_waited_the_lease_out_analyses_untracked():
    sb = FakeSB()
    _claim(sb, "gen-slow")
    _, v = _claim(sb, "gen-b", waited=ac.lease_seconds())
    assert (v.action, v.held) == ("analyse", False)
    assert _rows(sb)[0]["holder_generation_id"] == "gen-slow", "the holder may still finish"


def test_the_rollback_lever_turns_it_off(monkeypatch):
    monkeypatch.setenv("BOOK_SHARED_ANALYSIS", "0")
    sb = FakeSB()
    _, v = _claim(sb, "gen-a")
    assert (v.action, v.held) == ("analyse", False) and _rows(sb) == []


def test_the_catalogue_lever_does_not_switch_books_off(monkeypatch):
    monkeypatch.setenv("CATALOGUE_SHARED_ANALYSIS", "0")
    assert ac.book_unit(BOOK, 1, 1, "ar").on is True
    assert ac.article_unit("art-1").on is False


# ── the worker wiring on the BOOK path (worker.process._build_from_analysis) ──

def _book_build(monkeypatch, sb, gen_id, kind, *, part=1, cumulative=False):
    """Run the real shared build for one book document, from a prepared
    ``_Unit`` (the book prelude — download, structure, OCR — is not the
    subject here), with the analysis and the document builder recorded."""
    import tempfile
    from tests.test_catalogue_kit import _StubClient, _fake_doc, _worker_env

    process, _uploads, analysis_calls = _worker_env(monkeypatch, sb)
    monkeypatch.setattr("worker.client.set_chapter_grounding",
                        lambda *a, **k: pytest.fail("a part analysis must never become the chapter's grounding"))
    seen: dict = {}
    monkeypatch.setattr("docgen.generate_document", _fake_doc(seen))
    gen = {"id": gen_id, "kind": kind, "status": "processing", "owner_id": "teacher-1", "book_id": BOOK,
           "chapter_ref": "1", "params": {"part": part, "language": "ar"}}
    job = {"id": f"job-{gen_id}", "type": kind, "status": "processing", "generation_id": gen_id,
           "book_id": BOOK, "params": {}, "progress": 20}
    sb.tables.setdefault("generations", []).append(dict(gen))
    sb.tables.setdefault("jobs", []).append(dict(job))
    chapter = {"chapter_num": 1, "title": "Materials", "key_boxes": [], "images": [],
               "sections": [{"section_title": "States", "content": CHUNK["text"], "subsections": []}]}
    with tempfile.TemporaryDirectory() as tmp:
        unit = process._Unit(book={"id": BOOK, "title": "Science 7", "language": "en"}, book_id=BOOK,
                             client=_StubClient(), chapter=chapter, chapter_num=1, chapter_title="Materials",
                             lesson_lang="ar", lesson_dir="rtl", jawi=False, is_cumulative=cumulative,
                             part_ref=part, part_total=6, part_chunk=dict(CHUNK), pk=dict(CHUNK))
        process._build_from_analysis(sb, job, gen_id, gen, unit, tmp,
                                     allow_premium=False, tier_info={}, canary_provider=None)
    return analysis_calls, seen


def test_two_part_documents_of_one_kit_analyse_once_through_the_worker(monkeypatch):
    sb = FakeSB()
    calls_1, seen_ws = _book_build(monkeypatch, sb, "gen-ws", "worksheet")
    calls_2, seen_act = _book_build(monkeypatch, sb, "gen-act", "activity")
    assert len(calls_1) == 1 and calls_1[0]["chunks_override"] == [CHUNK], "the part is analysed verbatim"
    assert calls_2 == [], "the activity read the worksheet's analysis instead of calling the model"
    assert seen_act["analysis"]["episodes"] == seen_ws["analysis"]["episodes"]
    assert seen_act["analysis"]["language"] == "ar"
    (row,) = _rows(sb)
    assert (row["unit_key"], row["status"], row["holder_generation_id"]) == (f"{BOOK}:1:1:ar", "ready", "gen-ws")


def test_a_sibling_arriving_mid_analysis_is_deferred_through_the_worker(monkeypatch):
    from worker import client as db
    sb = FakeSB()
    _claim(sb, "gen-holder", chunks=(CHUNK,))
    # the worker hashes the chunk exactly as _claim does, so this is the same unit and text
    with pytest.raises(db.DeferredJob) as exc:
        _book_build(monkeypatch, sb, "gen-act", "activity")
    assert exc.value.note == "waiting for the chapter part's analysis (generation gen-hold)"


def test_a_cumulative_paper_never_touches_the_lease(monkeypatch):
    sb = FakeSB()
    try:
        _book_build(monkeypatch, sb, "gen-exam", "worksheet", part=None, cumulative=True)
    except Exception:  # noqa: BLE001 — a cumulative build wants more than this stub unit; only the lease matters
        pass
    assert _rows(sb) == []


def test_a_missing_table_degrades_to_analysing():
    """A worker deployed before 0144 is applied must build exactly as before."""
    class Broken:
        def table(self, name):
            raise RuntimeError('relation "public.book_unit_analyses" does not exist')

    _, v = _claim(Broken(), "gen-a")
    assert (v.action, v.held) == ("analyse", False) and "cache unavailable" in v.reason
