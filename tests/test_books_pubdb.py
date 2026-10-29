"""The shared publishing database (publishing/runner/pubdb.py): push/pull are hash-checked and a stale writer is refused.
No Postgres here — the dump is opaque bytes to the sync logic; the database round trip is checked on the runner."""
import gzip
import json

import pytest

from publishing.runner import files, pubdb, stages
from publishing.runner.store import FileStore


def _dump(tmp_path, name, text):
    p = tmp_path / name
    p.write_bytes(gzip.compress(text.encode(), mtime=0))
    return p


def test_first_push_then_pull_round_trips(tmp_path):
    b = files.DirBucket(tmp_path / "bucket")
    m = pubdb.push(b, _dump(tmp_path, "a.sql.gz", "create schema pub;"), "pc:test", None)
    assert m["previous_sha"] is None and m["source"] == "pc:test"
    got = pubdb.pull(b, tmp_path / "pulled.sql.gz")
    assert got["sha256"] == m["sha256"]
    assert gzip.decompress((tmp_path / "pulled.sql.gz").read_bytes()) == b"create schema pub;"


def test_a_writer_that_pulled_an_older_copy_is_refused(tmp_path):
    b = files.DirBucket(tmp_path / "bucket")
    first = pubdb.push(b, _dump(tmp_path, "a.sql.gz", "v1"), "pc", None)
    pubdb.push(b, _dump(tmp_path, "b.sql.gz", "v2 from run 20"), "run:20", first["sha256"])
    with pytest.raises(pubdb.Conflict, match="changed since it was pulled"):
        pubdb.push(b, _dump(tmp_path, "c.sql.gz", "v2 from the pc"), "pc", first["sha256"])
    assert json.loads((tmp_path / "bucket" / pubdb.MANIFEST_KEY).read_text())["source"] == "run:20"


def test_a_second_first_push_is_refused(tmp_path):
    b = files.DirBucket(tmp_path / "bucket")
    pubdb.push(b, _dump(tmp_path, "a.sql.gz", "v1"), "pc", None)
    with pytest.raises(pubdb.Conflict):
        pubdb.push(b, _dump(tmp_path, "b.sql.gz", "other v1"), "pc2", None)


def test_the_manifest_is_replaced_only_if_unchanged(tmp_path):
    b = files.DirBucket(tmp_path / "bucket")
    pubdb.push(b, _dump(tmp_path, "a.sql.gz", "v1"), "pc", None)
    with pytest.raises(pubdb.PreconditionFailed):
        b.put_if_match(pubdb.MANIFEST_KEY, b"{}", "application/json", "not-the-etag")
    with pytest.raises(pubdb.PreconditionFailed):
        b.put_if_match(pubdb.MANIFEST_KEY, b"{}", "application/json", None)     # "only if absent", but it exists


def test_pull_refuses_a_dump_that_does_not_match_its_manifest(tmp_path):
    b = files.DirBucket(tmp_path / "bucket")
    pubdb.push(b, _dump(tmp_path, "a.sql.gz", "v1"), "pc", None)
    (tmp_path / "bucket" / pubdb.DUMP_KEY).write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="does not match"):
        pubdb.pull(b, tmp_path / "x.sql.gz")


def test_pull_before_any_push_says_what_to_do(tmp_path):
    with pytest.raises(RuntimeError, match="push one from the PC first"):
        pubdb.pull(files.DirBucket(tmp_path / "bucket"), tmp_path / "x.sql.gz")


def test_one_database_writer_at_a_time(tmp_path):
    p = tmp_path / "store.json"
    p.write_text(json.dumps({"pub_works": [], "pub_runs": [
        {"id": 1, "stage_key": "readiness", "status": "running"},
        {"id": 2, "stage_key": "coverage", "status": "running"},
        {"id": 3, "stage_key": "readiness", "status": "done"}]}))
    s = FileStore(p)
    assert s.others_running(9, stages.WRITES_PUBDB) == [1]
    assert s.others_running(1, stages.WRITES_PUBDB) == []


def test_the_readiness_stage():
    stage, params = stages.validate("readiness", {})
    assert params == {} and not stage.needs_source and stage.uses_pubdb and stage.writes_pubdb
    assert "readiness" in stages.WRITES_PUBDB and "coverage" not in stages.WRITES_PUBDB


def test_readiness_summary_carries_the_gaps(tmp_path):
    (tmp_path / "readiness.json").write_text(json.dumps({
        "pct": 25.0, "ready": False, "outstanding_by_state": {"missing_topic": 1},
        "gaps": [{"title": "Calculating Speed", "topic_id": None, "steps": [{"order": 1, "step": "Create the topic"}]}],
        "curriculum": {"id": "c1", "code": "x", "name": "X"}}))
    usd, lines, summary = stages.STAGES["readiness"].collect(tmp_path)
    assert usd == 0.0 and lines == []
    assert summary["gaps"][0]["title"] == "Calculating Speed" and summary["curriculum"]["id"] == "c1"
