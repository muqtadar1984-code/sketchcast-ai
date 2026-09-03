"""A storage transfer cut by the peer is retried, not failed.

THE INCIDENT (prod, 2026-09-03 11:50). A teacher generated a full kit — six
jobs claimed within three seconds, six threads each downloading the same 30 MB
textbook from Supabase storage over HTTP/2. The edge reset one of the streams:

    httpx.RemoteProtocolError: <StreamReset stream_id:1, error_code:2, remote_reset:True>
      File "/app/worker/client.py", line 633, in download_book
        data = sb.storage.from_("uploads").download(storage_path)

That worksheet failed at progress 0, two seconds into a job that had done no
work, for a blip that was over before the log line was written. There was no
retry anywhere on the path. The same call shape uploads a rendered lesson at
the END of a ten-minute render, where a reset would cost the whole render.

Both transfers are safe to repeat: a download has no side effect, and the
upload is an upsert onto a deterministic path. So the worker now retries a
TRANSPORT failure (httpx.TransportError — reset, timeout, disconnect) or a
storage 5xx, three attempts with backoff, and still fails at once on anything
that a retry cannot fix (a missing object, a forbidden bucket, a bug).

These are behavioural tests against a fake Supabase client; `time.sleep` is
captured so the backoff is asserted, not waited for.
"""

from __future__ import annotations

import httpx
import pytest
from storage3.utils import StorageException

from worker import client as db


class _Bucket:
    """One bucket whose download/upload raise the queued exceptions first."""

    def __init__(self, failures=()):
        self.failures = list(failures)
        self.download_calls = 0
        self.upload_calls = []

    def download(self, path):
        self.download_calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return b"%PDF-1.4 fake"

    def upload(self, dest, payload, opts):
        self.upload_calls.append((dest, payload, dict(opts)))
        if self.failures:
            raise self.failures.pop(0)
        return {"Key": dest}


class _Storage:
    def __init__(self, bucket):
        self._bucket = bucket

    def from_(self, name):
        return self._bucket


class _SB:
    def __init__(self, bucket):
        self.storage = _Storage(bucket)


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(db.time, "sleep", lambda s: slept.append(s))
    return slept


def _reset():
    return httpx.RemoteProtocolError("<StreamReset stream_id:1, error_code:2, remote_reset:True>")


# ── download ────────────────────────────────────────────────────────────


def test_a_reset_download_is_retried_and_the_book_lands(tmp_path, no_sleep):
    bucket = _Bucket([_reset()])
    dest = tmp_path / "book.pdf"
    out = db.download_book(_SB(bucket), "owner/book.pdf", dest)
    assert out == dest and dest.read_bytes().startswith(b"%PDF")
    assert bucket.download_calls == 2
    assert no_sleep == [2.0], "one failure → one backoff, the short one"


def test_two_resets_then_success_uses_both_backoffs(tmp_path, no_sleep):
    bucket = _Bucket([_reset(), httpx.ReadTimeout("read timed out")])
    db.download_book(_SB(bucket), "owner/book.pdf", tmp_path / "book.pdf")
    assert bucket.download_calls == 3
    assert no_sleep == [2.0, 5.0]


def test_three_resets_gives_up_with_the_transport_error(tmp_path, no_sleep):
    """Bounded: a peer that is genuinely down must not be hammered forever, and
    the job's error must name what was measured — the transport failure."""
    bucket = _Bucket([_reset(), _reset(), _reset()])
    with pytest.raises(httpx.RemoteProtocolError):
        db.download_book(_SB(bucket), "owner/book.pdf", tmp_path / "book.pdf")
    assert bucket.download_calls == 3
    assert not (tmp_path / "book.pdf").exists(), "nothing is written on failure"


def test_a_missing_object_is_not_retried(tmp_path, no_sleep):
    """A 404 is an answer, not a blip: retrying it would only delay the same
    failure by seven seconds and hide that the upload never reached storage
    (Sara's 2026-07 incident)."""
    bucket = _Bucket([StorageException({"statusCode": "404", "error": "not_found",
                                        "message": "Object not found"})])
    with pytest.raises(StorageException):
        db.download_book(_SB(bucket), "owner/missing.pdf", tmp_path / "book.pdf")
    assert bucket.download_calls == 1 and no_sleep == []


def test_a_storage_5xx_is_retried(tmp_path, no_sleep):
    bucket = _Bucket([StorageException({"statusCode": "502", "error": "Bad Gateway",
                                        "message": "upstream"})])
    db.download_book(_SB(bucket), "owner/book.pdf", tmp_path / "book.pdf")
    assert bucket.download_calls == 2


def test_a_bug_is_not_retried(tmp_path, no_sleep):
    bucket = _Bucket([TypeError("not a transport problem")])
    with pytest.raises(TypeError):
        db.download_book(_SB(bucket), "owner/book.pdf", tmp_path / "book.pdf")
    assert bucket.download_calls == 1 and no_sleep == []


# ── upload ──────────────────────────────────────────────────────────────


def test_a_reset_upload_is_retried_with_upsert(tmp_path, no_sleep):
    """The video is uploaded at the END of a render; a reset there must not
    throw the render away. Every attempt carries upsert so a retry overwrites
    its own partial predecessor rather than failing on 'already exists'."""
    src = tmp_path / "lesson.mp4"
    src.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    bucket = _Bucket([_reset()])
    out = db.upload_artifact(_SB(bucket), src, "owner/gen/lesson.mp4")
    assert out == "owner/gen/lesson.mp4"
    assert len(bucket.upload_calls) == 2
    for dest, payload, opts in bucket.upload_calls:
        assert dest == "owner/gen/lesson.mp4"
        assert payload == src.read_bytes()
        assert opts["upsert"] == "true" and opts["content-type"] == "video/mp4"
    assert no_sleep == [2.0]


def test_the_module_imports_what_the_backoff_calls():
    """The backoff calls time.sleep at module scope. The first cut of this fix
    was written into a working tree where ANOTHER session's uncommitted block
    had already added `import time`, so every test passed while the COMMIT
    itself would have raised NameError on the first retry in production. A
    passing suite in a shared tree proves the tree, not the commit: this pins
    the import in the module's own source, and the commit was re-verified from
    `git archive HEAD` in an empty directory."""
    import re
    from pathlib import Path

    src = Path(db.__file__).read_text(encoding="utf-8")
    assert re.search(r"^import time$", src, re.M), "worker/client.py must import time itself"
    assert hasattr(db, "time") and callable(db.time.sleep)


def test_the_classifier_names_exactly_the_transient_shapes():
    ok = db._is_transient_transfer_error
    assert ok(httpx.RemoteProtocolError("reset"))
    assert ok(httpx.ConnectError("refused"))
    assert ok(httpx.ReadError("eof"))
    assert ok(httpx.WriteTimeout("slow"))
    assert ok(StorageException({"statusCode": "503", "message": "unavailable"}))
    assert ok(StorageException({"statusCode": 500, "message": "boom"}))
    assert not ok(StorageException({"statusCode": "403", "message": "forbidden"}))
    assert not ok(httpx.HTTPStatusError("400", request=None, response=None))
    assert not ok(ValueError("nope"))
    assert not ok(KeyError("storage_path"))
