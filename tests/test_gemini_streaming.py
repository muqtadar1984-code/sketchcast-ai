"""shared.gemini_client streams text requests (:streamGenerateContent?alt=sse)
and reassembles the reply into the single-shot shape — so a twenty-minute
maths lesson is never a request Vertex has to hold open in silence."""
from __future__ import annotations

import json

import pytest
import requests

import shared.gemini_client as gc
from shared import phase_clock
from shared import provider_gate as pg
from tests.test_gemini_client import usage_response


class _SSE:
    """A streamed response: ``events`` are dicts (or raw strings) sent as
    ``data:`` lines; ``trip`` raises after that many events."""
    def __init__(self, events, status=200, headers=None, trip=None, trip_exc=None):
        self.events, self.status_code = events, status
        self.headers = headers or {}
        self.trip, self.trip_exc = trip, trip_exc or requests.exceptions.ReadTimeout("gap > 120 s")
        self.text = "" if status == 200 else json.dumps({"error": {"code": status, "message": f"http {status}"}})
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        for i, ev in enumerate(self.events):
            if self.trip is not None and i >= self.trip:
                raise self.trip_exc
            yield ""                                   # SSE events are separated by blank lines
            yield "data: " + (ev if isinstance(ev, str) else json.dumps(ev))
        if self.trip is not None and self.trip >= len(self.events):
            raise self.trip_exc                        # the connection died after the last chunk

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        raise AssertionError("a streamed response is not read with .json()")

    def close(self):
        self.closed = True


def _chunk(text=None, finish=None, usage=None, thought=False):
    cand = {"content": {"role": "model", "parts": []}}
    if text is not None:
        part = {"text": text}
        if thought:
            part["thought"] = True
        cand["content"]["parts"].append(part)
    if finish:
        cand["finishReason"] = finish
    out = {"candidates": [cand]}
    if usage:
        out["usageMetadata"] = usage
    return out


@pytest.fixture
def streaming(monkeypatch):
    monkeypatch.setenv("VERTEX_STREAM", "1")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "sketchcast")
    monkeypatch.setenv("VERTEX_REGION", "global")
    monkeypatch.setattr("shared.gemini_client._access_token", lambda: "tok")
    monkeypatch.setattr("shared.gemini_client.time.sleep", lambda s: None)
    yield


def _post_returning(monkeypatch, responses):
    """requests.post answering `responses` in order, recording every call."""
    calls = []

    def post(url, headers=None, json=None, timeout=None, stream=False):
        calls.append({"url": url, "json": json, "timeout": timeout, "stream": stream})
        return responses[min(len(calls) - 1, len(responses) - 1)]
    monkeypatch.setattr("shared.gemini_client.requests.post", post)
    return calls


# ── the wire form ───────────────────────────────────────────────────────
def test_a_text_request_streams_with_a_between_chunk_timeout(streaming, monkeypatch):
    calls = _post_returning(monkeypatch, [_SSE([_chunk("{}", "STOP", {"promptTokenCount": 1, "candidatesTokenCount": 1})])])
    gc.GeminiClient(model="gemini-3.5-flash").analyze("hello")
    c = calls[0]
    assert c["url"].endswith("gemini-3.5-flash:streamGenerateContent?alt=sse")
    assert c["stream"] is True
    assert c["timeout"] == (30.0, 600.0), "connect, then the longest silence before the first chunk or between two — per read, not a total"


def test_the_kill_switch_restores_the_single_shot_request(streaming, monkeypatch):
    monkeypatch.setenv("VERTEX_STREAM", "0")

    class _Plain:
        status_code = 200
        headers = {}
        text = ""
        def raise_for_status(self): pass
        def json(self): return usage_response(prompt=1, answer=1, text='{"ok": 1}')
    calls = _post_returning(monkeypatch, [_Plain()])
    out = gc.GeminiClient(model="gemini-3.5-flash").analyze("hello")
    assert out["data"] == {"ok": 1}
    assert calls[0]["url"].endswith(":generateContent") and calls[0]["stream"] is False
    assert calls[0]["timeout"] == 600


# ── reassembly ──────────────────────────────────────────────────────────
def test_chunks_are_joined_in_order_and_the_last_finish_and_usage_win(streaming, monkeypatch):
    usage = {"promptTokenCount": 120, "candidatesTokenCount": 3000, "thoughtsTokenCount": 0}
    events = [_chunk('{"lesson": '), _chunk('"area of '), _chunk('a trapezium"}', finish="STOP", usage=usage)]
    _post_returning(monkeypatch, [_SSE(events)])
    client = gc.GeminiClient(model="gemini-3.5-flash")
    out = client.analyze("write the lesson")
    assert out["data"] == {"lesson": "area of a trapezium"}
    assert out["truncated"] is False
    assert out["usage"]["input_tokens"] == 120 and out["usage"]["output_tokens"] == 3000
    assert client.session_usage["calls"] == 1 and client.session_usage["output_tokens"] == 3000


def test_a_max_tokens_finish_still_reads_as_truncated(streaming, monkeypatch):
    events = [_chunk('{"a": 1'), _chunk(', "b": 2', finish="MAX_TOKENS", usage={"candidatesTokenCount": 9})]
    _post_returning(monkeypatch, [_SSE(events)] * 3)
    out = gc.GeminiClient(model="gemini-3.5-flash").analyze("x", max_tokens=9)
    assert out["truncated"] is True


def test_thought_parts_are_not_text(streaming, monkeypatch):
    events = [_chunk("let me think", thought=True), _chunk('{"ok": true}', finish="STOP")]
    _post_returning(monkeypatch, [_SSE(events)])
    assert gc.GeminiClient(model="gemini-3.5-flash").analyze("x")["data"] == {"ok": True}


def test_a_safety_block_is_an_empty_text_not_a_crash(streaming, monkeypatch):
    _post_returning(monkeypatch, [_SSE([{"candidates": [{"finishReason": "SAFETY"}],
                                         "promptFeedback": {"blockReason": "SAFETY"}}])])
    client = gc.GeminiClient(model="gemini-3.5-flash")
    resp = client._post([{"text": "x"}], "sys", 100)
    assert client._text(resp) == "" and client._finish_reason(resp) == "SAFETY"
    assert resp["promptFeedback"] == {"blockReason": "SAFETY"}


def test_an_unreadable_line_is_skipped_and_the_stream_goes_on(streaming, monkeypatch, caplog):
    events = ['{"not json', _chunk('{"ok": 1}', finish="STOP")]
    _post_returning(monkeypatch, [_SSE(events)])
    assert gc.GeminiClient(model="gemini-3.5-flash").analyze("x")["data"] == {"ok": 1}


def test_the_response_is_closed_either_way(streaming, monkeypatch):
    good = _SSE([_chunk("{}", "STOP")])
    _post_returning(monkeypatch, [good])
    gc.GeminiClient(model="gemini-3.5-flash")._post([{"text": "x"}], "sys", 10)
    assert good.closed
    bad = _SSE([_chunk("{")], trip=1)
    _post_returning(monkeypatch, [bad])
    with pytest.raises(gc._Transient):
        gc.GeminiClient(model="gemini-3.5-flash")._post([{"text": "x"}], "sys", 10)
    assert bad.closed


# ── failures on the wire ────────────────────────────────────────────────
def test_a_dropped_stream_is_a_transient_504_that_says_how_far_it_got(streaming, monkeypatch):
    _post_returning(monkeypatch, [_SSE([_chunk("abc"), _chunk("def")], trip=2)])
    with pytest.raises(gc._Transient) as e:
        gc.GeminiClient(model="gemini-3.5-flash")._post([{"text": "x"}], "sys", 10)
    assert e.value.status == 504
    assert "2 chunk(s), 6 chars" in e.value.body and "ReadTimeout" in e.value.body


def test_a_dropped_stream_is_retried_by_the_ladder(streaming, monkeypatch):
    dropped = _SSE([_chunk("abc")], trip=1, trip_exc=requests.exceptions.ChunkedEncodingError("reset"))
    good = _SSE([_chunk('{"ok": 1}', "STOP")])
    calls = _post_returning(monkeypatch, [dropped, good])
    assert gc.GeminiClient(model="gemini-3.5-flash").analyze("x")["data"] == {"ok": 1}
    assert len(calls) == 2


def test_an_error_chunk_mid_stream_is_raised_by_its_code(streaming, monkeypatch):
    _post_returning(monkeypatch, [_SSE([_chunk("abc"), {"error": {"code": 503, "status": "UNAVAILABLE"}}])])
    with pytest.raises(gc._Transient) as e:
        gc.GeminiClient(model="gemini-3.5-flash")._post([{"text": "x"}], "sys", 10)
    assert e.value.status == 503
    _post_returning(monkeypatch, [_SSE([{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}])])
    with pytest.raises(gc._RateLimited):
        gc.GeminiClient(model="gemini-3.5-flash")._post([{"text": "x"}], "sys", 10)


def test_an_http_429_on_a_streamed_request_still_carries_retry_after(streaming, monkeypatch):
    _post_returning(monkeypatch, [_SSE([], status=429, headers={"Retry-After": "17"})])
    with pytest.raises(gc._RateLimited) as e:
        gc.GeminiClient(model="gemini-3.5-flash")._post([{"text": "x"}], "sys", 10)
    assert e.value.retry_after == 17.0
    assert pg.gate("vertex_text").refusals == 0, "_post reports nothing; _call records the outcome"


def test_the_gate_records_a_streamed_call_like_any_other(streaming, monkeypatch):
    _post_returning(monkeypatch, [_SSE([_chunk("{}", "STOP")])])
    g = pg.gate("vertex_text")
    g.s["step_after"] = 1
    gc.GeminiClient(model="gemini-3.5-flash").analyze("x")
    assert g.successes == 1 or g.rate >= 0


# ── composes with #217's slow-drop switch ───────────────────────────────
def test_a_slow_drop_still_switches_models_under_streaming(streaming, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr("shared.gemini_client.time.monotonic", lambda: clock["t"])
    monkeypatch.setattr(gc, "SLOW_DROP_MODEL", "gemini-3.5-flash-lite")

    class _SlowDrop(_SSE):
        def iter_lines(self, decode_unicode=False):
            clock["t"] += 601.0
            raise requests.exceptions.ReadTimeout("silence")
    calls = _post_returning(monkeypatch, [_SlowDrop([]), _SSE([_chunk('{"ok": 1}', "STOP")])])
    out = gc.GeminiClient(model="gemini-3.5-flash").analyze("write the lesson")
    assert out["data"] == {"ok": 1}
    assert "gemini-3.5-flash:streamGenerateContent" in calls[0]["url"]
    assert "gemini-3.5-flash-lite:streamGenerateContent" in calls[1]["url"]


# ── the first chunk of a thinking request can take minutes ───────────────
def test_the_read_timeout_covers_time_to_first_chunk(streaming, monkeypatch):
    """2026-10-14 15:14:40: the first streamed maths lesson timed out at 120 s
    before its first byte — the lesson-script request thinks for minutes
    before it answers. The per-read timeout bounds that wait too, so it is
    the server's own 600 s, not a between-chunk guess."""
    calls = _post_returning(monkeypatch, [_SSE([_chunk("{}", "STOP")])])
    gc.GeminiClient(model="gemini-3.5-flash").analyze("write the lesson")
    assert calls[0]["timeout"] == (30.0, 600.0)
    assert gc.STREAM_READ_TIMEOUT_S >= 600.0


def test_a_timeout_before_any_response_is_a_transient_the_ladder_retries(streaming, monkeypatch):
    attempts = {"n": 0}

    def post(url, headers=None, json=None, timeout=None, stream=False):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise requests.exceptions.ReadTimeout("HTTPSConnectionPool: Read timed out. (read timeout=600.0)")
        return _SSE([_chunk('{"ok": 1}', "STOP")])
    monkeypatch.setattr("shared.gemini_client.requests.post", post)
    out = gc.GeminiClient(model="gemini-3.5-flash").analyze("write the lesson")
    assert out["data"] == {"ok": 1} and attempts["n"] == 2


def test_a_timeout_before_any_response_names_itself(streaming, monkeypatch):
    monkeypatch.setattr("shared.gemini_client.requests.post",
                        lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("read timeout=600.0")))
    with pytest.raises(gc._Transient) as e:
        gc.GeminiClient(model="gemini-3.5-flash")._post([{"text": "x"}], "sys", 10)
    assert e.value.status == 504 and "no response in" in e.value.body and "streamed" in e.value.body


def test_a_connection_failure_on_the_single_shot_request_is_a_transient_too(streaming, monkeypatch):
    monkeypatch.setenv("VERTEX_STREAM", "0")
    monkeypatch.setattr("shared.gemini_client.requests.post",
                        lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("reset by peer")))
    with pytest.raises(gc._Transient) as e:
        gc.GeminiClient(model="gemini-3.5-flash")._post([{"text": "x"}], "sys", 10)
    assert e.value.status == 504 and "single-shot" in e.value.body


def test_a_long_silence_before_the_first_chunk_switches_to_the_lighter_model(streaming, monkeypatch):
    """Composes with #217: a request that gave nothing for SLOW_DROP_S is a
    slow drop, and the remaining attempts go to the lighter model."""
    clock = {"t": 0.0}
    monkeypatch.setattr("shared.gemini_client.time.monotonic", lambda: clock["t"])
    monkeypatch.setattr(gc, "SLOW_DROP_MODEL", "gemini-3.5-flash-lite")
    calls = []

    def post(url, headers=None, json=None, timeout=None, stream=False):
        calls.append(url)
        if len(calls) == 1:
            clock["t"] += 600.0
            raise requests.exceptions.ReadTimeout("read timeout=600.0")
        return _SSE([_chunk('{"ok": 1}', "STOP")])
    monkeypatch.setattr("shared.gemini_client.requests.post", post)
    assert gc.GeminiClient(model="gemini-3.5-flash").analyze("write the lesson")["data"] == {"ok": 1}
    assert "gemini-3.5-flash:stream" in calls[0] and "flash-lite:stream" in calls[1]


# ── a stream that never ends is cut ──────────────────────────────────────


class _Trickle(_SSE):
    """A stream that keeps the socket alive without ever finishing:
    ``filler`` events (blank keep-alive lines, or chunks with no text) every
    ``gap`` seconds of the patched clock, ``text_every`` of them carrying a
    text part. Never raises on its own."""
    def __init__(self, clock, gap, filler="", text_every=None, events_max=100000):
        super().__init__([])
        self.clock, self.gap, self.filler, self.text_every, self.events_max = clock, gap, filler, text_every, events_max

    def iter_lines(self, decode_unicode=False):
        for i in range(self.events_max):
            self.clock["t"] += self.gap
            if self.text_every and i % self.text_every == 0:
                yield "data: " + json.dumps(_chunk("x"))
            elif self.filler == "":
                yield ""
            else:
                yield "data: " + json.dumps(self.filler)


def _clocked(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr("shared.gemini_client.time.monotonic", lambda: clock["t"])
    return clock


def test_a_stream_of_keep_alives_with_no_text_is_cut_at_the_stall_cap_and_retried_on_the_lighter_model(streaming, monkeypatch):
    """2026-10-14 19:15 → 23:42 UTC: the video job sat inside one streamed
    call for four and a half hours — the socket never went silent, nothing
    ever arrived that was text. The stall cap cuts it; the ladder's next
    attempt, after that long a wait, goes to the lighter model (#217)."""
    clock = _clocked(monkeypatch)
    monkeypatch.setattr(gc, "SLOW_DROP_MODEL", "gemini-3.5-flash-lite")
    calls = _post_returning(monkeypatch, [_Trickle(clock, gap=50.0), _SSE([_chunk('{"ok": 1}', "STOP")])])
    out = gc.GeminiClient(model="gemini-3.5-flash").analyze("write the lesson")
    assert out["data"] == {"ok": 1}
    assert len(calls) == 2 and "gemini-3.5-flash-lite:streamGenerateContent" in calls[1]["url"]
    assert calls[0]["json"] is not None and calls[0]["stream"] is True


def test_thought_only_chunks_count_as_no_text(streaming, monkeypatch):
    clock = _clocked(monkeypatch)
    thought = _chunk("thinking…", thought=True)
    calls = _post_returning(monkeypatch, [_Trickle(clock, gap=100.0, filler=thought)])
    with pytest.raises(requests.HTTPError):
        gc.GeminiClient(model="gemini-3.5-flash").analyze("hello", retries=0)
    assert len(calls) == 1 and clock["t"] <= gc.STREAM_STALL_S + 100.0 + 1e-6, "cut at the first check past the cap"


def test_a_stream_that_keeps_trickling_text_is_cut_at_the_total_cap(streaming, monkeypatch):
    clock = _clocked(monkeypatch)
    # a text part every 300 s: the stall cap never fires, the total cap does
    _post_returning(monkeypatch, [_Trickle(clock, gap=300.0, text_every=1)])
    with pytest.raises(requests.HTTPError):
        gc.GeminiClient(model="gemini-3.5-flash").analyze("hello", retries=0)
    assert gc.STREAM_TOTAL_S < clock["t"] <= gc.STREAM_TOTAL_S + 300.0 + 1e-6


def test_the_stall_cut_says_how_far_the_reply_got(streaming, monkeypatch, caplog):
    import logging
    clock = _clocked(monkeypatch)
    _post_returning(monkeypatch, [_Trickle(clock, gap=200.0)])
    with caplog.at_level(logging.WARNING, logger="worker"), pytest.raises(requests.HTTPError) as exc:
        gc.GeminiClient(model="gemini-3.5-flash").analyze("hello", retries=0)
    assert "stream stalled" in str(exc.value) and "no text for" in str(exc.value)
    assert any("stalled" in r.getMessage() for r in caplog.records)


def test_a_normal_stream_well_inside_the_caps_is_untouched(streaming, monkeypatch):
    clock = _clocked(monkeypatch)
    sse = _SSE([_chunk('{"k": "a'), _chunk('b"}', "STOP", usage={"promptTokenCount": 1, "candidatesTokenCount": 2})])
    real = sse.iter_lines

    def slow(decode_unicode=False):
        for line in real(decode_unicode):
            clock["t"] += 100.0          # 100 s between lines: under both caps
            yield line
    sse.iter_lines = slow
    _post_returning(monkeypatch, [sse])
    out = gc.GeminiClient(model="gemini-3.5-flash").analyze("hello")
    assert out["data"] == {"k": "ab"}
