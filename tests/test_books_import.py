"""The book importer: complete revisions from partial snapshots, and approval hashes re-verified."""
import json

from publishing.tools import books_import as bi


def _doc(text, gate=None):
    d = {"chapter": {"number": 1, "title": "T"}, "plan": {}, "work": {"curriculum": "c"},
         "sections": [{"sid": "s1", "heading": "H", "blocks": [{"type": "paragraph", "text": text, "ref": None}]}],
         "exercises": [{"xid": "x1", "stem": "Q?"}]}
    if gate:
        d["gate_d"] = gate
    return d


def _write(p, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def test_absent_chapter_is_carried_forward_from_the_next_revision(tmp_path):
    cfg = {"chapters": [1, 2], "revisions": [{"key": "a", "snapshot": "old/chNN.json"},
                                             {"key": "b", "snapshot": "chNN.json"}]}
    _write(tmp_path / "ch01.json", _doc("one new"))
    _write(tmp_path / "ch02.json", _doc("two"))
    _write(tmp_path / "old" / "ch01.json", _doc("one old"))          # only chapter 1 changed in revision b
    docs = bi.revision_docs(tmp_path, cfg)
    assert docs["a"][1][0]["sections"][0]["blocks"][0]["text"] == "one old"
    assert docs["a"][2][0]["sections"][0]["blocks"][0]["text"] == "two"   # unchanged → same text as b
    assert docs["a"][2][1] == "ch02.json"


def test_hash_rules_match_the_engine():
    d = _doc("x")
    body = json.dumps({"sections": d["sections"], "exercises": d["exercises"]}, sort_keys=True, ensure_ascii=False)
    import hashlib
    assert bi.canonical_sha(d) == hashlib.sha256(body.encode()).hexdigest()
    stamped = {**d, "gate_d": {"status": "approved"}}
    assert bi.whole_file_sha(stamped) == bi.whole_file_sha(d)        # the stamp itself is not hashed


def test_findings_status_follows_the_corrections_record():
    audit = {"findings": [{"severity": "P1", "where": "s1.b0"}, {"severity": "P2"}, {"severity": "P1"}]}
    corr = {"applied": [{"id": "ch03-F000", "what": "fixed", "unit": "s1"}], "resolved": ["ch03-F000"],
            "rulings": {"ch03-F001": {"verdict": "disagree"}}, "accepted": ["ch03-F000"]}
    out = bi._findings("w", "r2", 3, audit, corr)
    assert [f["status"] for f in out] == ["applied", "rejected", "deferred"]
    assert out[0]["decision"]["verified"] is True


def test_file_kinds_match_whole_words():
    # "log" inside "Biology" once filed every full manuscript as a report
    assert bi._kind_of("revision-4-1/SketchCast Biology - Cells - Revision 4.1 (digital).pdf") == "book_pdf"
    assert bi._kind_of("rev4/Revision 4 - Correction log.pdf") == "qa_report"
    assert bi._kind_of("revision-4-1/Revision 4.1 - Addendum and QA.pdf") == "qa_report"
    assert bi._kind_of("cells-revision-4.1-kdp-interior.pdf") == "kdp"
