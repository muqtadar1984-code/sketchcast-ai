"""The books runner (publishing/runner) end to end on a file store and a folder bucket — no production, no models."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from publishing.runner import __main__ as runner, files, stages
from publishing.runner.store import FileStore, RestStore
from publishing.tools.books_import import canonical_sha

SLUG = "tiny-book"


def _book_files(stamp_ok: bool = True) -> dict[str, bytes]:
    doc = {"chapter": {"title": "Cells"}, "sections": [{"sid": "s1", "heading": "H", "blocks": []}], "exercises": []}
    doc["gate_d"] = {"status": "approved", "version": "r1",
                     "content_sha256": canonical_sha(doc) if stamp_ok else "0" * 64}
    out = {"ch01-draft.json": json.dumps(doc).encode()}
    for k in ("dossier", "dossier-full", "diagnostics"):
        out[f"ch01-{k}.json"] = b"{}"
    for p in ("annotations.json", "approvals.json", "kdp.json", "rev2/audit/objectives-0893.json"):
        out[p] = b"{}"
    return out


def _bucket(tmp: Path, book: dict[str, bytes], corrupt: str | None = None) -> files.DirBucket:
    b = files.DirBucket(tmp / "bucket")
    prefix = f"books/{SLUG}/source/"
    manifest = []
    for rel, body in book.items():
        b.put(prefix + rel, b"tampered" if rel == corrupt else body, "application/json")
        manifest.append({"rel": rel, "key": prefix + rel, "bytes": len(body), "sha256": files.sha256(body)})
    b.put(prefix + files.MANIFEST, json.dumps({"files": manifest}).encode(), "application/json")
    return b


def _store(tmp: Path, stage_key: str, params: dict | None = None) -> FileStore:
    p = tmp / "store.json"
    p.write_text(json.dumps({"pub_works": [{"uid": "w1", "slug": SLUG}],
                             "pub_runs": [{"id": 7, "work_uid": "w1", "stage_key": stage_key, "status": "queued",
                                           "params": params or {}, "detail": {}}]}))
    return FileStore(p)


def _run(store: FileStore) -> dict:
    return json.loads(store.path.read_text())


def test_check_files_run_end_to_end(tmp_path):
    store = _store(tmp_path, "check_files")
    bucket = _bucket(tmp_path, _book_files())
    assert runner.execute(store, bucket, 7, tmp_path / "work") == "done"
    d = _run(store)
    run = d["pub_runs"][0]
    assert run["status"] == "done" and run["detail"]["summary"]["approved_matching"] == 1
    assert [a["rel_path"] for a in d["pub_artifacts"]] == ["runs/run-7/check.json"]
    assert (tmp_path / "bucket" / f"books/{SLUG}/runs/run-7/check.json").exists()
    assert any("pulled 8 files" in e["message"] for e in d["pub_run_events"])
    assert d["pub_run_steps"][0]["status"] == "done"


def test_a_stamp_mismatch_fails_the_check(tmp_path):
    store = _store(tmp_path, "check_files")
    assert runner.execute(store, _bucket(tmp_path, _book_files(stamp_ok=False)), 7, tmp_path / "work") == "failed"
    d = _run(store)
    assert "no longer matches its Gate D stamp" in d["pub_runs"][0]["error"]
    assert d["pub_artifacts"], "the failed run's evidence is still uploaded"


def test_a_corrupt_source_file_stops_the_run_before_the_command(tmp_path):
    store = _store(tmp_path, "check_files")
    status = runner.execute(store, _bucket(tmp_path, _book_files(), corrupt="kdp.json"), 7, tmp_path / "work")
    assert status == "failed"
    run = _run(store)["pub_runs"][0]
    assert "does not match its manifest hash" in run["error"] and not _run(store).get("pub_artifacts")


def test_a_run_is_claimed_once(tmp_path):
    store = _store(tmp_path, "check_files")
    bucket = _bucket(tmp_path, _book_files())
    assert runner.execute(store, bucket, 7, tmp_path / "w1") == "done"
    assert runner.execute(store, bucket, 7, tmp_path / "w2") == "skipped"


def test_unknown_stage_and_parameters_are_refused():
    with pytest.raises(ValueError):
        stages.validate("drop_tables", {})
    with pytest.raises(ValueError):
        stages.validate("coverage", {"first": 1, "last": 10, "shell": "rm"})
    with pytest.raises(ValueError):
        stages.validate("coverage", {"first": 5, "last": 2})
    assert stages.validate("coverage", {})[1] == {"first": 1, "last": 10}


def test_coverage_collect_reads_cost_and_counts(tmp_path):
    (tmp_path / "coverage.json").write_text(json.dumps({"1": {"usd": 0.4, "validators": {"g": {}, "o": {}}},
                                                         "2": {"usd": 0.6, "validators": {"g": {}, "o": {}}}}))
    (tmp_path / "coverage-matrix.json").write_text(json.dumps({"a": {"status": "taught and assessed"},
                                                                "b": {"status": "not covered"}}))
    usd, lines, summary = stages.STAGES["coverage"].collect(tmp_path)
    assert usd == 1.0 and [ln["chapter"] for ln in lines] == [1, 2]
    assert summary == {"counts": {"taught and assessed": 1, "not covered": 1}, "objectives": 2}


def test_cancel_from_the_app_stops_the_command(tmp_path, monkeypatch):
    slow = stages.Stage(key="slow", title="Slow", step="slow",
                        argv=lambda slug, p, rel: [sys.executable, "-c",
                                                   "import time\nfor i in range(100):\n print(i, flush=True); time.sleep(0.1)"])
    monkeypatch.setitem(stages.STAGES, "slow", slow)
    monkeypatch.setattr(runner, "HEARTBEAT_S", 0.2)
    store = _store(tmp_path, "slow")

    def press_cancel():
        time.sleep(0.8)
        store.update(7, cancel_requested_at="now")
    threading.Thread(target=press_cancel, daemon=True).start()
    assert runner.execute(store, _bucket(tmp_path, _book_files()), 7, tmp_path / "work") == "cancelled"
    assert _run(store)["pub_runs"][0]["status"] == "cancelled"


def test_the_rest_store_refuses_writes_outside_the_run_tables():
    s = RestStore(url="https://example.invalid", key="k")
    for method, path in (("PATCH", "/pub_chapter_revisions"), ("DELETE", "/pub_runs"), ("POST", "/pub_approvals"),
                         ("GET", "/jobs"), ("POST", "/jobs"), ("PATCH", "/pub_artifacts")):
        with pytest.raises(RuntimeError, match="refuses"):
            s.http.request(method, path, json={})


def test_audit_collect_reads_cost_per_validator_and_counts_findings(tmp_path):
    (tmp_path / "audit").mkdir()
    (tmp_path / "audit" / "ch01-audit.json").write_text(json.dumps({
        "chapter": 1, "validators": {"gemini": {"usd": 0.16, "in": 10, "out": 20}, "opus": {"usd": 0.37, "in": 11, "out": 21}},
        "findings": [{"severity": "P1", "agreement": "SINGLE"}, {"severity": "P0", "agreement": "CONFIRMED"}]}))
    (tmp_path / "audit" / "ch02-audit.json").write_text(json.dumps({
        "chapter": 2, "validators": {"gemini": {"usd": 0.1}}, "findings": [{"severity": "P2", "agreement": "CONFIRMED"}]}))
    usd, lines, summary = stages.STAGES["academic_audit"].collect(tmp_path)
    assert usd == 0.63 and len(lines) == 3 and lines[0]["input_tokens"] == 10
    assert summary == {"chapters": 2, "findings": {"P0": 1, "P1": 1, "P2": 1, "confirmed": 2, "single": 1}}


def test_audit_writes_into_the_run_folder():
    stage, params = stages.validate("academic_audit", {"first": 2, "last": 3})
    assert stage.env("b", params, "runs/run-9") == {"PUBLISH_REV": "runs/run-9"}
    assert stage.argv("b", params, "runs/run-9")[-4:-2] == ["2", "3"]
