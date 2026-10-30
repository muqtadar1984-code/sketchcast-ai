"""Book releases for the Library and SK (publishing/runner/release.py; contract BOOK-RELEASES-CONTRACT-2026-10-30)."""
import json

import pytest

from publishing.runner import book_chapters as bc, files, stages
from publishing.runner.release import draft_chapters, edition_key, publish_release
from publishing.runner.store import FileStore

W = "lin"


def _doc(n, text):
    return {"chapter": {"number": n, "title": f"Chapter {n}", "objectives": [{"code": "7Ae.01", "text": "letters"}]},
            "plan": {"sections": []}, "sections": [{"sid": "s1", "blocks": [{"type": "paragraph", "text": text}]}],
            "exercises": [{"xid": "x1", "stem": "?", "answer": "4"}], "review": {"ready_for_gate_d": True},
            "videos": ["video:abc"]}


def _pdf(text="page"):
    import fitz
    d = fitz.open()
    d.new_page().insert_text((72, 72), text)
    return d.tobytes()


def _setup(tmp_path, approved=(1,)):
    bucket = files.DirBucket(tmp_path / "bucket")
    rows, approvals = [], []
    for n in (1, 2):
        doc = _doc(n, f"text {n}")
        rel = f"runs/run-{20 + n}/ch{n:02d}-draft.json"
        bucket.put(f"books/{W}/{rel}", json.dumps(doc).encode(), "application/json")
        bucket.put(f"books/{W}/runs/run-{20 + n}/ch{n:02d}.pdf", _pdf(f"ch{n}"), "application/pdf")
        bucket.put(f"books/{W}/runs/run-{20 + n}/ch{n:02d}-dossier-full.json",
                   json.dumps({"topics": [{"slug": f"t{n}", "title": "T", "article": {"id": "a", "version": 1}}]}).encode(),
                   "application/json")
        row = bc.chapter_row(W, "d1", doc, {}, rel)
        rows.append(row)
        if n in approved:
            approvals.append({"id": 100 + n, "work_uid": W, "kind": "gate_d", "chapter": n,
                              "content_sha256": row["content_sha256"], "approved_by": "founder", "approved_on": "2026-10-30"})
    p = tmp_path / "store.json"
    p.write_text(json.dumps({"pub_works": [{"uid": W, "slug": W, "title": "Algebra", "language": "en",
                                            "current_revision": "d1", "subject_strand": "Maths"}],
                             "pub_runs": [], "pub_book_revisions": [{"work_uid": W, "key": "d1", "ordinal": 0}],
                             "pub_chapter_revisions": rows, "pub_approvals": approvals}))
    return FileStore(p), bucket, rows


RUN = {"id": 40, "work_uid": W, "requested_by": "founder@sketchcast.app"}


def test_edition_keys():
    assert edition_key("final", "en", ["en-1", "en-draft-2026-10-30-1"], "2026-10-30") == "en-2"
    assert edition_key("draft", "en", ["en-draft-2026-10-30-1"], "2026-10-30") == "en-draft-2026-10-30-2"
    assert edition_key("draft", "ms", [], "2026-10-30") == "ms-draft-2026-10-30-1"


def test_a_draft_holds_only_the_approved_chapters_and_is_re_run_as_more_are_approved(tmp_path):
    store, bucket, rows = _setup(tmp_path, approved=(1,))
    assert [r["chapter"] for r, _ in draft_chapters(rows, store.approvals(W))] == [1]
    got = publish_release(store, bucket, RUN, W, {"final": 0})
    assert got["release_kind"] == "draft" and got["chapters"] == [1] and got["supersedes"] is None
    b = json.loads(bucket.get(got["bundle_key"]))
    assert b["schema"] == "sketchcast.book-release/1" and b["work"]["revision_key"] == "d1"
    ch = b["chapters"][0]
    assert ch["approval"]["id"] == 101 and ch["exercises"][0]["answer"] == "4" and ch["videos"] == ["video:abc"]
    assert ch["topics"][0]["slug"] == "t1" and ch["pdf"]["key"].endswith(f"editions/{got['edition_key']}/ch01.pdf")
    # the same chapters again: refused
    with pytest.raises(RuntimeError, match="already holds exactly these chapters"):
        publish_release(store, bucket, RUN, W, {"final": 0})
    # chapter 2 approved: a new draft that supersedes the first
    d = json.loads((tmp_path / "store.json").read_text())
    d["pub_approvals"].append({"id": 102, "work_uid": W, "kind": "gate_d", "chapter": 2,
                               "content_sha256": rows[1]["content_sha256"]})
    (tmp_path / "store.json").write_text(json.dumps(d))
    again = publish_release(store, bucket, RUN, W, {"final": 0})
    assert again["chapters"] == [1, 2] and again["supersedes"] == got["release_id"]
    live = [r for r in store.releases(W) if not r["superseded_by"]]
    assert [r["id"] for r in live] == [again["release_id"]]


def test_a_final_needs_gate_e_and_carries_the_compiled_pdf_and_cover(tmp_path):
    store, bucket, rows = _setup(tmp_path, approved=(1, 2))
    with pytest.raises(RuntimeError, match="no Gate E approval"):
        publish_release(store, bucket, RUN, W, {"final": 1})
    pdf = _pdf("book")
    bucket.put(f"books/{W}/runs/run-30/book.pdf", pdf, "application/pdf")
    d = json.loads((tmp_path / "store.json").read_text())
    d["pub_runs"].append({"id": 30, "work_uid": W, "stage_key": "compile_book", "status": "done",
                          "detail": {"summary": {"pdf": "book.pdf", "pdf_sha256": files.sha256(pdf), "pages": 1,
                                                 "chapters": [{"number": n, "content_sha256": r["content_sha256"]}
                                                              for n, r in ((1, rows[0]), (2, rows[1]))]}}})
    d["pub_approvals"].append({"id": 200, "work_uid": W, "kind": "gate_e", "revision_key": "d1",
                               "content_sha256": files.sha256(pdf)})
    (tmp_path / "store.json").write_text(json.dumps(d))
    got = publish_release(store, bucket, RUN, W, {"final": 1})
    assert got["release_kind"] == "final" and got["edition_key"] == "en-1" and got["chapters"] == [1, 2]
    b = json.loads(bucket.get(got["bundle_key"]))
    assert b["release"]["gate_e_approval_id"] == 200 and b["files"]["digital_pdf"]["sha256"] == files.sha256(pdf)
    assert b["work"]["cover"]["path"].endswith("cover.png") and bucket.get(b["work"]["cover"]["path"])[:4] == b"\x89PNG"
    with pytest.raises(RuntimeError, match="already released"):
        publish_release(store, bucket, RUN, W, {"final": 1})


def test_release_stage_is_registered():
    s = stages.STAGES["release"]
    assert s.makes_release and not s.uses_models and not s.writes_pubdb
    assert stages.validate("release", {"final": 1})[1] == {"final": 1, "reissue": 0}
    with pytest.raises(ValueError):
        stages.validate("release", {"final": 2})
    with pytest.raises(ValueError):
        stages.validate("release", {"final": 1, "reissue": 3})


def _book_pdf_with_bookmarks(chapters=(1, 2)):
    import fitz
    d = fitz.open()
    toc = [[1, "Cover", 1]]
    d.new_page().insert_text((72, 72), "cover")
    for n in chapters:
        start = d.page_count + 1
        for p in range(2):
            d.new_page().insert_text((72, 72), f"chapter {n} page {p + 1}")
        toc.append([1, f"Chapter {n}: Title {n}", start])
    d.new_page().insert_text((72, 72), "index")
    toc.append([1, "Index", d.page_count])
    d.set_toc(toc)
    return d.tobytes()


def test_a_final_slices_chapter_pdfs_from_the_book_and_can_be_reissued(tmp_path):
    import fitz
    from publishing.runner.release import chapter_slices
    pdf = _book_pdf_with_bookmarks()
    sl = chapter_slices(pdf)
    assert sorted(sl) == [1, 2] and all(fitz.open(stream=b, filetype="pdf").page_count == 2 for b in sl.values())
    store, bucket, rows = _setup(tmp_path, approved=(1, 2))
    bucket.put(f"books/{W}/runs/run-30/book.pdf", pdf, "application/pdf")
    d = json.loads((tmp_path / "store.json").read_text())
    d["pub_runs"].append({"id": 30, "work_uid": W, "stage_key": "compile_book", "status": "done",
                          "detail": {"summary": {"pdf": "book.pdf", "pdf_sha256": files.sha256(pdf), "pages": 6,
                                                 "chapters": [{"number": r["chapter"], "content_sha256": r["content_sha256"]}
                                                              for r in rows]}}})
    d["pub_approvals"].append({"id": 200, "work_uid": W, "kind": "gate_e", "revision_key": "d1", "content_sha256": files.sha256(pdf)})
    (tmp_path / "store.json").write_text(json.dumps(d))
    first = publish_release(store, bucket, RUN, W, {"final": 1})
    b = json.loads(bucket.get(first["bundle_key"]))
    ch2 = fitz.open(stream=bucket.get(b["chapters"][1]["pdf"]["key"]), filetype="pdf")   # the approved book's own pages
    assert ch2.page_count == 2 and "chapter 2 page 1" in ch2[0].get_text()
    again = publish_release(store, bucket, RUN, W, {"final": 1, "reissue": 1})
    assert again["edition_key"] == "en-1.1" and again["supersedes"] == first["release_id"]
    assert [r["edition_key"] for r in store.releases(W) if not r["superseded_by"]] == ["en-1.1"]


def test_an_imported_book_finds_its_extras_through_the_source_manifest(tmp_path):
    """Biology's rows carry the PC's source_path; the files live under books/<slug>/source/ (the manifest)."""
    store, bucket, rows = _setup(tmp_path, approved=(1, 2))
    d = json.loads((tmp_path / "store.json").read_text())
    for r in d["pub_chapter_revisions"]:
        n = r["chapter"]
        src = r["source_path"]
        bucket.put(f"books/{W}/source/ch{n:02d}-draft.json", bucket.get(f"books/{W}/{src}"), "application/json")
        bucket.put(f"books/{W}/source/ch{n:02d}-dossier-full.json",
                   bucket.get(f"books/{W}/{src.rsplit('/', 1)[0]}/ch{n:02d}-dossier-full.json"), "application/json")
        r["source_path"] = f"rev4-1/ch{n:02d}-draft.json"                   # a PC path that is not in the bucket
    (tmp_path / "store.json").write_text(json.dumps(d))
    bucket.put(f"books/{W}/source/{files.MANIFEST}", json.dumps({"files": [
        {"rel": f"ch{n:02d}-{f}", "key": f"books/{W}/source/ch{n:02d}-{f}"} for n in (1, 2)
        for f in ("draft.json", "dossier-full.json")]}).encode(), "application/json")
    got = publish_release(store, bucket, RUN, W, {"final": 0})
    b = json.loads(bucket.get(got["bundle_key"]))
    assert b["chapters"][0]["videos"] == ["video:abc"] and b["chapters"][0]["topics"][0]["slug"] == "t1"


def test_a_book_approval_of_the_revision_covers_its_chapters(tmp_path):
    store, bucket, rows = _setup(tmp_path, approved=())
    d = json.loads((tmp_path / "store.json").read_text())
    d["pub_approvals"].append({"id": 7, "work_uid": W, "kind": "book", "scope": "book", "revision_key": "d1"})
    (tmp_path / "store.json").write_text(json.dumps(d))
    got = publish_release(store, bucket, RUN, W, {"final": 0})
    assert got["chapters"] == [1, 2]
    b = json.loads(bucket.get(got["bundle_key"]))
    assert b["chapters"][0]["approval"]["kind"] == "book" and b["chapters"][0]["approval"]["id"] == 7
    row = store.releases(W)[-1]["chapters"][0]
    assert row["approval_kind"] == "book" and row["gate_d_approval_id"] is None and row["approval_id"] == 7
