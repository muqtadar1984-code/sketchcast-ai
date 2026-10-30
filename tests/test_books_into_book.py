"""A chapter drafted on the books runner joins the book (publishing/runner/book_chapters.py)."""
import json

from publishing.runner import book_chapters as bc, files, stages
from publishing.runner.store import FileStore

W = "lin"


def _doc(n=1, text="Letters stand for numbers."):
    return {"chapter": {"number": n, "title": f"Chapter {n}"}, "plan": {"sections": []},
            "sections": [{"sid": "s1", "blocks": [{"type": "paragraph", "text": text}]}],
            "exercises": [{"xid": "x1", "stem": "What is x?"}], "review": {"ready_for_gate_d": True}}


def test_placement_rules():
    assert bc.placement([], set(), 1) == ("d1", {"key": "d1", "ordinal": 0, "label": "Draft 1 (from the app)",
                                                 "summary": "Opened when chapter 1 was drafted on the books runner"}, False)
    # the app's own latest revision without this chapter: add to it
    assert bc.placement([{"key": "d1", "ordinal": 0}], {1}, 2) == ("d1", None, False)
    # this chapter is already in it: a new revision, carrying the others forward
    key, row, carry = bc.placement([{"key": "d1", "ordinal": 0}], {1, 2}, 1)
    assert (key, row["ordinal"], carry) == ("d2", 1, True)
    # an imported book (Biology r4.1): never added to an imported revision
    key, row, carry = bc.placement([{"key": "r4", "ordinal": 4}, {"key": "r4.1", "ordinal": 5}], {1, 2, 3}, 2)
    assert (key, row["ordinal"], carry) == ("d1", 6, True)


def test_manifest_keeps_other_files_and_replaces_by_rel():
    m = {"files": [{"rel": "ch01-draft.json", "key": "old"}, {"rel": "kdp.json", "key": "k"}], "extra": 1}
    out = bc.manifest_with(m, [{"rel": "ch01-draft.json", "key": "new"}, {"rel": "ch02-draft.json", "key": "n2"}])
    assert [f["key"] for f in out["files"]] == ["new", "n2", "k"] and out["extra"] == 1
    assert bc.manifest_with(None, [{"rel": "a", "key": "b"}]) == {"files": [{"rel": "a", "key": "b"}]}


def _store(tmp, **tables):
    p = tmp / "store.json"
    p.write_text(json.dumps({"pub_works": [{"uid": W, "slug": W, "status": "scope_approved"}], "pub_runs": [], **tables}))
    return FileStore(p)


def test_an_approved_chapter_is_never_redrafted(tmp_path):
    row = bc.chapter_row(W, "d1", _doc(), {}, "x")
    store = _store(tmp_path, pub_book_revisions=[{"work_uid": W, "key": "d1", "ordinal": 0}],
                   pub_chapter_revisions=[row],
                   pub_approvals=[{"work_uid": W, "kind": "gate_d", "chapter": 1, "content_sha256": row["content_sha256"]}])
    assert "approved at Gate D" in bc.refusal(store, W, 1)
    assert bc.refusal(store, W, 2) is None
    # an approval of an OLDER text does not block a chapter whose current text differs
    store2 = _store(tmp_path, pub_book_revisions=[{"work_uid": W, "key": "d1", "ordinal": 0}], pub_chapter_revisions=[row],
                    pub_approvals=[{"work_uid": W, "kind": "gate_d", "chapter": 1, "content_sha256": "older"}])
    assert bc.refusal(store2, W, 1) is None


def test_publish_places_the_chapter_and_adds_it_to_the_source_set(tmp_path):
    book = tmp_path / "book"
    out = book / "runs" / "run-9"
    out.mkdir(parents=True)
    (out / "ch02-draft.json").write_text(json.dumps(_doc(2, "Like terms combine.")))
    (out / "ch02-dossier-full.json").write_text(json.dumps({"topics": [], "library_assets": []}))
    ch1 = bc.chapter_row(W, "d1", _doc(1), {}, "runs/run-8/ch01-draft.json")
    store = _store(tmp_path, pub_book_revisions=[{"work_uid": W, "key": "d1", "ordinal": 0}],
                   pub_chapter_revisions=[ch1])
    bucket = files.DirBucket(tmp_path / "bucket")
    got = bc.publish(store, bucket, W, W, book, "runs/run-9", 2)
    assert got["revision"] == "d1" and not got["new_revision"] and got["source_files"] == 2
    assert sorted(c["chapter"] for c in store.chapter_rows(W, "d1")) == [1, 2]
    work = json.loads((tmp_path / "store.json").read_text())["pub_works"][0]
    assert work["current_revision"] == "d1" and work["status"] == "drafting"   # Gate D approves in d1
    m = json.loads(bucket.get(f"books/{W}/source/{files.MANIFEST}"))
    assert [f["rel"] for f in m["files"]] == ["ch02-dossier-full.json", "ch02-draft.json"]
    assert m["files"][1]["key"] == f"books/{W}/runs/run-9/ch02-draft.json"
    # re-drafting chapter 2: a new revision d2 with chapter 1 carried forward unchanged
    (out / "ch02-draft.json").write_text(json.dumps(_doc(2, "Like terms combine, unlike terms do not.")))
    got = bc.publish(store, bucket, W, W, book, "runs/run-9", 2)
    assert got["revision"] == "d2" and got["new_revision"] and got["carried_forward"] == 1
    d2 = {c["chapter"]: c for c in store.chapter_rows(W, "d2")}
    assert d2[1]["content_sha256"] == ch1["content_sha256"] and d2[2]["content_sha256"] != got.get("x")


def test_draft_chapter_stage_writes_the_book_and_the_database():
    s = stages.STAGES["draft_chapter"]
    assert s.into_book and s.writes_pubdb and s.uses_models and s.source_optional and "draft_chapter" in stages.WRITES_PUBDB
    assert not stages.STAGES["draft_trial"].into_book


def test_adopt_trial_takes_only_a_current_trial_and_costs_nothing(tmp_path):
    from publishing.runner.adopt import stale
    s = stages.STAGES["adopt_trial"]
    assert s.into_book and s.writes_pubdb and not s.uses_models
    assert [k for k, _ in s.prefetch("lin", {"run": 27, "chapter": 1})] == [
        "books/lin/runs/run-27/ch01-draft.json", "books/lin/runs/run-27/ch01-dossier-full.json",
        "books/lin/runs/run-27/ch01-dossier.json"]
    assert stale({"edition_id": 2, "pin_run_id": 9}, {"id": 2, "pin_run_id": 9}) is None
    assert "draft the chapter again" in stale({"edition_id": 2, "pin_run_id": 8}, {"id": 2, "pin_run_id": 9})
    assert "Gate R first" in stale({"edition_id": 2, "pin_run_id": 9}, None)
    (tmp_path / "draft.json").write_text(json.dumps({"chapter": 1, "usd": 0.0, "drafted_for_usd": 2.32}))
    assert s.collect(tmp_path)[:2] == (0.0, [])


def test_compile_book_stage_is_free_and_reads_the_source_set(tmp_path):
    s = stages.STAGES["compile_book"]
    assert not s.uses_models and s.needs_source and s.needs_assets and not s.writes_pubdb
    assert s.argv("lin", {"first": 1, "last": 3}, "runs/run-1")[-2:] == ["1", "3"]
    (tmp_path / "compile.json").write_text(json.dumps({"pdf": "b.pdf", "pages": 14, "chapters": [], "missing": [2]}))
    assert s.collect(tmp_path) == (0.0, [], {"pdf": "b.pdf", "pages": 14, "chapters": [], "missing": [2]})


def test_sync_chapter_merges_the_edited_text_and_keeps_everything_else():
    from publishing.runner.sync_chapter import merged
    from publishing.tools.books_import import canonical_sha
    draft = {**_doc(2, "old text"), "checks": [{"x": 1}], "work": {"slug": W}, "costs": [{"usd": 1}]}
    edited = _doc(2, "new text")
    row = bc.chapter_row(W, "d2", edited, {}, "runs/run-50/ch02-draft.json")
    doc = merged(draft, row["body"])
    assert canonical_sha(doc) == row["content_sha256"] and doc["checks"] == [{"x": 1}] and doc["work"] == {"slug": W}
    s = stages.STAGES["sync_chapter"]
    assert s.syncs_chapter and not s.uses_models and s.needs_source and s.needs_assets


def test_sync_chapter_adds_pictures_replaced_in_the_app_as_sources():
    from publishing.runner.sync_chapter import with_uploads
    from publishing.tools.books_import import figure_map
    dossier = {"topics": [{"figures": [{"id": "fig:a"}]}], "library_assets": [{"id": "lib:b", "_asset": {"storage_path": "x.png"}}]}
    figures = {
        "fig:a": {"store": "visual-assets", "path": "a.png"},
        "edit:img7": {"store": "s3", "path": "books/bio/edits/u.png", "kind": "photo", "caption": "New", "size": [40, 30], "credit": {"note": "n"}},
        "book:hero": {"store": "s3", "path": "books/_shared/book-assets/hero.png"},
    }
    out = with_uploads(dossier, figures)
    added = [a for a in out["library_assets"] if a["id"] == "edit:img7"]
    assert len(out["library_assets"]) == 2 and added
    assert added[0]["_asset"] == {"s3_key": "books/bio/edits/u.png", "vision": {"w": 40, "h": 30}}
    assert added[0]["parts"] == [] and added[0]["kind"] == "photo"
    assert with_uploads(out, figures) == out                       # added once, never twice
    assert dossier["library_assets"] == [{"id": "lib:b", "_asset": {"storage_path": "x.png"}}]   # the input is untouched


def test_figure_map_names_a_replaced_picture_by_its_books_bucket_key(tmp_path):
    import json
    from publishing.tools.books_import import figure_map
    d = {"topics": [], "library_assets": [{"id": "edit:img7", "kind": "photo", "description": "New. More",
                                           "_asset": {"s3_key": "books/bio/edits/u.png", "vision": {"w": 4, "h": 3}}}]}
    (tmp_path / "ch02-dossier-full.json").write_text(json.dumps(d), encoding="utf-8")
    f = figure_map(tmp_path, "bio")["edit:img7"]
    assert f["store"] == "s3" and f["path"] == "books/bio/edits/u.png" and f["kind"] == "photo"


def test_a_replaced_picture_is_read_from_the_books_bucket_as_png(monkeypatch):
    import io
    from PIL import Image
    from publishing.manuscript import render
    from publishing.runner import files
    buf = io.BytesIO()
    Image.new("RGB", (12, 8), "red").save(buf, "JPEG")

    class FakeBucket:
        def get(self, key):
            assert key == "books/bio/edits/u.jpg"
            return buf.getvalue()

    monkeypatch.setattr(files, "default_bucket", FakeBucket)
    png = render.asset_bytes(None, "", {"s3_key": "books/bio/edits/u.jpg"})
    assert png[:4] == b"\x89PNG" and Image.open(io.BytesIO(png)).size == (12, 8)


def test_front_matter_edited_in_the_app_latest_accepted_text_wins():
    from publishing.runner.compile import front_matter_overrides
    from publishing.manuscript.book import ABOUT_INTRO, IMPRINT_NOTE, front_matter_texts
    edits = [{"id": 3, "unit": "preface", "after_text": "Second preface. "}, {"id": 1, "unit": "preface", "after_text": "First"},
             {"id": 2, "unit": "imprint", "after_text": "  "}, {"id": 4, "unit": "cover", "after_text": "not a piece"}]
    fm = front_matter_overrides(edits)
    assert fm == {"preface": "Second preface."}
    assert front_matter_texts(fm, "engine preface") == {"preface": "Second preface.", "imprint": IMPRINT_NOTE, "about": ABOUT_INTRO}
    assert front_matter_texts({}, "engine preface")["preface"] == "engine preface"


def test_compile_reads_the_front_matter_first():
    from publishing.runner.stages import STAGES
    assert STAGES["compile_book"].front_matter and not STAGES["draft_chapter"].front_matter
