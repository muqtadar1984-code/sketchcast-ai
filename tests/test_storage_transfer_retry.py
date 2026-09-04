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

Two kinds of test here, deliberately:

  * FAKE-CLIENT tests drive download_book / upload_artifact with a bucket that
    raises whatever we hand it — they pin the retry LOOP (attempt counts,
    backoff schedule, what is written when).
  * REAL-LIBRARY tests push the exception through storage3 2.31.0's own
    SyncBucketProxy._request over an httpx.MockTransport — they pin the
    CLASSIFIER against the shapes the library actually raises. The first cut of
    this fix matched "'statusCode': '503'" inside the exception text; the
    library's message is "{'statusCode': 503, …}" (unquoted, one string arg,
    the status on .status), a non-JSON 5xx body escapes as JSONDecodeError, and
    a gateway's {"message": …} body dies in a KeyError→AttributeError chain.
    Nine fake-shaped tests were green while every real 5xx got one attempt
    (adversarial review, 2026-09-03). Never again: the 5xx tests below
    construct nothing by hand — the library raises, the classifier decides.

`time.sleep` is captured so the backoff is asserted, not waited for.
"""

from __future__ import annotations

import httpx
import pytest
from storage3._sync.file_api import SyncBucketProxy
from storage3.exceptions import StorageApiError
from yarl import URL

from worker import client as db


# ── fakes ───────────────────────────────────────────────────────────────


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


def _real_bucket(responses):
    """storage3's REAL SyncBucketProxy over an httpx.MockTransport that answers
    each request with the next canned response. The exceptions that reach
    download_book are exactly the library's own."""
    queue = list(responses)
    calls = []

    def handler(request):
        calls.append(request)
        return queue.pop(0)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    proxy = SyncBucketProxy("uploads", URL("https://x.supabase.co/storage/v1"), {"apikey": "k"}, client)
    return proxy, calls


def _ok_pdf():
    return httpx.Response(200, content=b"%PDF-1.4 real")


@pytest.fixture
def no_sleep(monkeypatch):
    """Capture the waits instead of sleeping, and pin the jitter draw to 0 so
    the loop tests assert the schedule's BASE figures. The jitter itself is
    tested separately with the draw pinned to other values."""
    slept = []
    monkeypatch.setattr(db.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(db.random, "random", lambda: 0.0)
    return slept


def _reset():
    return httpx.RemoteProtocolError("<StreamReset stream_id:1, error_code:2, remote_reset:True>")


# ── the retry loop (fake client) ────────────────────────────────────────


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


def test_a_bug_is_not_retried(tmp_path, no_sleep):
    bucket = _Bucket([TypeError("not a transport problem")])
    with pytest.raises(TypeError):
        db.download_book(_SB(bucket), "owner/book.pdf", tmp_path / "book.pdf")
    assert bucket.download_calls == 1 and no_sleep == []


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


# ── the classifier, against what storage3 REALLY raises ─────────────────


def _download_via_real_lib(responses, tmp_path):
    proxy, calls = _real_bucket(responses)
    sb = _SB(proxy)
    dest = tmp_path / "book.pdf"
    return db.download_book(sb, "owner/book.pdf", dest), calls, dest


@pytest.mark.parametrize(
    "label,first",
    [
        ("503 storage-api JSON, statusCode as string",
         httpx.Response(503, json={"statusCode": "503", "error": "ServiceUnavailable", "message": "upstream"})),
        ("500 storage-api JSON, statusCode as int",
         httpx.Response(500, json={"statusCode": 500, "error": "InternalError", "message": "boom"})),
        ("502 edge HTML body (JSONDecodeError escapes storage3)",
         httpx.Response(502, text="<html><body>502 Bad Gateway</body></html>", headers={"content-type": "text/html"})),
        ("503 gateway JSON without storage keys (KeyError→AttributeError chain)",
         httpx.Response(503, json={"message": "name resolution failed"})),
        ("504 empty body",
         httpx.Response(504)),
    ],
)
def test_a_real_storage_5xx_is_retried(label, first, tmp_path, no_sleep):
    data, calls, dest = _download_via_real_lib([first, _ok_pdf()], tmp_path)
    assert dest.read_bytes() == b"%PDF-1.4 real", label
    assert len(calls) == 2, f"{label}: expected one retry"
    assert no_sleep == [2.0], label


@pytest.mark.parametrize(
    "label,first",
    [
        ("404 Object not found",
         httpx.Response(404, json={"statusCode": "404", "error": "not_found", "message": "Object not found"})),
        ("400 InvalidRequest",
         httpx.Response(400, json={"statusCode": "400", "error": "InvalidRequest", "message": "bad"})),
        ("409 Duplicate",
         httpx.Response(409, json={"statusCode": "409", "error": "Duplicate", "message": "exists"})),
        ("403 non-JSON body",
         httpx.Response(403, text="forbidden", headers={"content-type": "text/plain"})),
    ],
)
def test_a_real_storage_4xx_is_not_retried(label, first, tmp_path, no_sleep):
    """A 4xx is an answer, not a blip: retrying it would only delay the same
    failure by seven seconds and hide that the upload never reached storage
    (Sara's 2026-07 incident). Whatever storage3 raises for it — its own
    StorageApiError or an escaped parse error — must surface on attempt 1."""
    with pytest.raises(Exception):
        _download_via_real_lib([first, _ok_pdf()], tmp_path)
    assert no_sleep == [], label


def test_a_real_mid_body_h2_reset_is_retried(tmp_path, no_sleep):
    """The incident's exact shape, through storage3's real download(): a
    response whose body raises httpcore.RemoteProtocolError(StreamReset) while
    httpx reads it. The library must surface httpx.RemoteProtocolError (not a
    partial bytes object), and the worker must retry it."""
    import h2.events
    import httpcore

    attempts = {"n": 0}

    class _ResetBody:
        def __iter__(self):
            yield b"%PDF-1.4 partial "
            raise httpcore.RemoteProtocolError(
                h2.events.StreamReset(stream_id=1, error_code=2, remote_reset=True))

        def close(self):
            pass

    class _GoodBody:
        def __iter__(self):
            yield b"%PDF-1.4 real"

        def close(self):
            pass

    class _Pool:
        def handle_request(self, request):
            attempts["n"] += 1
            body = _ResetBody() if attempts["n"] == 1 else _GoodBody()
            return httpcore.Response(200, headers=[(b"content-length", b"13")], content=body)

    transport = httpx.HTTPTransport()
    transport._pool = _Pool()
    proxy = SyncBucketProxy("uploads", URL("https://x.supabase.co/storage/v1"), {"apikey": "k"},
                            httpx.Client(transport=transport))
    dest = tmp_path / "book.pdf"
    db.download_book(_SB(proxy), "owner/book.pdf", dest)
    assert dest.read_bytes() == b"%PDF-1.4 real", "the retry's bytes, never the partial ones"
    assert attempts["n"] == 2 and no_sleep == [2.0]


def test_the_classifier_on_the_library_objects_themselves():
    ok = db._is_transient_transfer_error
    # Transport failures, all httpx.TransportError subclasses.
    assert ok(httpx.RemoteProtocolError("reset"))
    assert ok(httpx.ConnectError("refused"))
    assert ok(httpx.ReadError("eof"))
    assert ok(httpx.WriteTimeout("slow"))
    # storage3's own error object: the status lives on .status, str or int.
    assert ok(StorageApiError("upstream", "ServiceUnavailable", "503"))
    assert ok(StorageApiError("boom", "InternalError", 500))
    assert not ok(StorageApiError("Object not found", "not_found", "404"))
    assert not ok(StorageApiError("exists", "Duplicate", 409))
    # A 5xx HTTPStatusError reached through an exception CHAIN (the escaped
    # JSONDecodeError / AttributeError cases) is transient; a 4xx one is not.
    req = httpx.Request("GET", "https://x/o")
    for code, expect in ((502, True), (504, True), (404, False), (403, False)):
        try:
            try:
                raise httpx.HTTPStatusError("s", request=req, response=httpx.Response(code, request=req))
            except httpx.HTTPStatusError:
                raise ValueError("Expecting value: line 1 column 1 (char 0)")
        except ValueError as chained:
            assert ok(chained) is expect, code
    # Not transient, no chain.
    assert not ok(ValueError("nope"))
    assert not ok(KeyError("storage_path"))
    assert not ok(httpx.HTTPStatusError("400", request=req, response=httpx.Response(400, request=req)))


# ── jitter ──────────────────────────────────────────────────────────────
#
# The incident was six threads reset by the same edge in the same second. On
# a fixed 2 s / 5 s schedule all six re-hit it in lock-step, the shape most
# likely to be reset again. Each wait is now the base plus a random share of
# up to half of it: the base stays the MINIMUM spacing, the retries spread.


def test_jitter_adds_up_to_half_the_base_and_never_less_than_the_base(monkeypatch):
    monkeypatch.setattr(db.random, "random", lambda: 0.0)
    assert db._transfer_delay(1) == 2.0 and db._transfer_delay(2) == 5.0
    monkeypatch.setattr(db.random, "random", lambda: 1.0)
    assert db._transfer_delay(1) == 3.0 and db._transfer_delay(2) == 7.5
    monkeypatch.setattr(db.random, "random", lambda: 0.5)
    assert db._transfer_delay(1) == 2.5 and db._transfer_delay(2) == 6.25
    # Attempts beyond the schedule reuse its last figure, as before.
    assert db._transfer_delay(3) == 6.25 and db._transfer_delay(9) == 6.25


def test_the_retry_loop_sleeps_the_jittered_figure(tmp_path, monkeypatch, caplog):
    slept = []
    monkeypatch.setattr(db.time, "sleep", lambda s: slept.append(s))
    draws = iter([0.25, 1.0])
    monkeypatch.setattr(db.random, "random", lambda: next(draws))
    bucket = _Bucket([_reset(), _reset()])
    with caplog.at_level("WARNING", logger="worker"):
        db.download_book(_SB(bucket), "owner/book.pdf", tmp_path / "book.pdf")
    assert slept == [2.25, 7.5]
    # The warning names the real wait. A broken format spec would not raise —
    # logging swallows it and prints to stderr — so the message is asserted.
    messages = [r.getMessage() for r in caplog.records if "retrying in" in r.getMessage()]
    assert len(messages) == 2, messages
    assert "retrying in 2.2s" in messages[0] and "attempt 1/3" in messages[0], messages[0]
    assert "retrying in 7.5s" in messages[1] and "attempt 2/3" in messages[1], messages[1]


def test_six_threads_do_not_retry_in_lock_step(tmp_path, monkeypatch):
    """The real draw, many times: every wait lands in [base, 1.5 × base] and
    the six are not all the same figure. (The chance that six real draws
    coincide to the millisecond is nil; a fixed schedule would give six 2.0s.)"""
    slept = []
    monkeypatch.setattr(db.time, "sleep", lambda s: slept.append(s))
    for _ in range(6):
        bucket = _Bucket([_reset()])
        db.download_book(_SB(bucket), "owner/book.pdf", tmp_path / "book.pdf")
    assert len(slept) == 6
    assert all(2.0 <= s <= 3.0 for s in slept), slept
    assert len({round(s, 6) for s in slept}) > 1, f"no spread: {slept}"


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
    assert re.search(r"^import random$", src, re.M), "worker/client.py must import random itself"
    assert hasattr(db, "time") and callable(db.time.sleep)
    assert hasattr(db, "random") and callable(db.random.random)
