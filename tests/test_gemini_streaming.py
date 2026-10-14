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
    assert c["timeout"] == (30.0, 120.0), "connect, then the longest silence between chunks — not a 600 s total"


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
