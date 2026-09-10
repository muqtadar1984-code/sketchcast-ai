"""A long reply must be streamed, because the SDK refuses to fetch one otherwise.

THE INCIDENT (prod, 2026-09-09 22:08–22:26 UTC). A teacher in Egypt signed up,
uploaded an Arabic KG2 book and generated her first kit. Six documents arrived.
The lesson VIDEO failed three times; she tried again and it failed three more.
Every attempt died at 0 % within seconds on:

    Streaming is required for operations that may take longer than 10 minutes.

That is not a timeout and not our code. anthropic/_base_client.py computes
`expected_time = 3600 * max_tokens / 128_000` and raises ValueError when it
passes 600 s, BEFORE sending anything — so above 21_333 output tokens a
non-streaming call cannot even be attempted.

Two facts had to meet. `shared/llm.client_for` routes Arabic to Claude (every
other language goes to Gemini, which has no such ceiling), and the semantic
path asks agent 3 for 30_000 tokens. So Arabic lessons — and only Arabic
lessons — became unservable the moment SEMANTIC_PLAN went back on, and nothing
reported it, because the reply never reached our code. The documents kept
working throughout, which is what made it look like a video bug rather than a
provider-routing one.

The fix puts the choice in _call_messages, the funnel every public method uses,
so analyze/analyze_image/analyze_images_batch/transcribe_images are all covered
and a future caller inherits it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared import claude_client as cc
from shared.claude_client import ClaudeClient


# ── the SDK's real ceiling ────────────────────────────────────────────────────
#
# The one test that matters if Anthropic changes its constants: our threshold is
# asserted against the SDK ACTUALLY INSTALLED, not against a number copied into
# a comment. A source assertion would have passed all through the incident.


def _sdk_refuses_at(max_tokens: int) -> bool:
    """Ask the installed SDK whether it would refuse this non-streaming call."""
    from anthropic import Anthropic

    client = Anthropic(api_key="test-key")
    try:
        client._calculate_nonstreaming_timeout(max_tokens, None)
    except TypeError:  # older SDK: single-argument signature
        try:
            client._calculate_nonstreaming_timeout(max_tokens)
        except ValueError:
            return True
        return False
    except ValueError:
        return True
    return False


def test_our_threshold_is_below_the_installed_sdk_ceiling():
    assert not _sdk_refuses_at(cc._NONSTREAMING_MAX_TOKENS), (
        f"the SDK refuses a non-streaming call at our own threshold "
        f"({cc._NONSTREAMING_MAX_TOKENS}); lower _NONSTREAMING_MAX_TOKENS"
    )


def test_the_budgets_agent3_really_asks_for_would_be_refused():
    """The premise of this whole file: 30_000 (semantic) and 32_000
    (conversational) are past the SDK's ceiling. If this ever fails, the SDK
    relaxed its rule and the streaming route became an optimisation rather than
    a necessity — worth knowing, not worth assuming."""
    assert _sdk_refuses_at(30_000)
    assert _sdk_refuses_at(32_000)


# ── fakes ─────────────────────────────────────────────────────────────────────


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


def _msg(text: str = '{"segments": [{"type": "hook"}]}', stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)], stop_reason=stop_reason, usage=_Usage(),
    )


def _client(monkeypatch) -> ClaudeClient:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = ClaudeClient()
    monkeypatch.setattr(ClaudeClient, "_log_usage", staticmethod(lambda usage: None))
    return c


def _spy(monkeypatch, c, *, create_raises=None):
    """Record which transport each call took."""
    seen = {"create": [], "stream": []}

    def create(system, messages, max_tokens):
        seen["create"].append(max_tokens)
        if create_raises is not None:
            raise create_raises
        return _msg()

    def stream(system, messages, max_tokens):
        seen["stream"].append(max_tokens)
        return _msg()

    monkeypatch.setattr(c, "_create", create)
    monkeypatch.setattr(c, "_create_stream", stream)
    return seen


# ── the fix ───────────────────────────────────────────────────────────────────


def test_the_production_budget_now_streams_instead_of_dying(monkeypatch):
    """Asmaa's exact call: the Arabic semantic script at 30_000 tokens."""
    c = _client(monkeypatch)
    seen = _spy(monkeypatch, c)

    out = c.analyze("write the script", max_tokens=30_000)

    assert out["data"]["segments"], "the reply must come back parsed"
    assert seen["stream"] == [30_000]
    assert seen["create"] == [], "a non-streaming call at 30k is refused by the SDK"


def test_a_small_budget_is_unchanged(monkeypatch):
    """The overwhelming majority of calls — every document, every vision call —
    must keep their existing non-streaming behaviour."""
    c = _client(monkeypatch)
    seen = _spy(monkeypatch, c)

    c.analyze("summarise", max_tokens=4096)

    assert seen["create"] == [4096] and seen["stream"] == []


def test_the_boundary_is_inclusive(monkeypatch):
    """20_000 still goes direct (a sibling test in test_truncation_and_title_gate
    pins that exact budget on the non-streaming path); one token more streams."""
    c = _client(monkeypatch)
    seen = _spy(monkeypatch, c)

    c.analyze("p", max_tokens=cc._NONSTREAMING_MAX_TOKENS)
    c.analyze("p", max_tokens=cc._NONSTREAMING_MAX_TOKENS + 1)

    assert seen["create"] == [cc._NONSTREAMING_MAX_TOKENS]
    assert seen["stream"] == [cc._NONSTREAMING_MAX_TOKENS + 1]


def test_a_model_with_a_lower_ceiling_falls_back(monkeypatch):
    """A model may declare its own max_nonstreaming_tokens below the formula's
    ceiling, which nothing on our side can predict. The refusal is caught and
    the call is remade streamed — nothing was sent, so it costs no tokens."""
    c = _client(monkeypatch)
    refusal = ValueError(
        "Streaming is required for operations that may take longer than 10 minutes."
    )
    seen = _spy(monkeypatch, c, create_raises=refusal)

    out = c.analyze("p", max_tokens=8000)

    assert out["data"]["segments"]
    assert seen["create"] == [8000], "it tried direct first"
    assert seen["stream"] == [8000], "…then streamed the same budget"


def test_the_predicate_matches_the_sdks_real_message():
    """Matched on text because the SDK raises a bare ValueError. Pin the real
    string, and refuse to match a merely similar one."""
    from anthropic import Anthropic

    try:
        Anthropic(api_key="k")._calculate_nonstreaming_timeout(30_000, None)
        pytest.fail("the SDK did not refuse 30_000 — see the ceiling test above")
    except ValueError as exc:
        assert cc._streaming_required(exc)

    assert not cc._streaming_required(ValueError("streaming is unavailable"))
    assert not cc._streaming_required(RuntimeError("Streaming is required"))
    assert not cc._streaming_required(ValueError("rate limited"))


def test_a_real_error_is_still_raised_not_streamed(monkeypatch):
    """The fallback must not become a catch-all: a deterministic failure has to
    surface, or a broken prompt would be retried on a second transport and cost
    twice as much before failing anyway."""
    c = _client(monkeypatch)
    seen = _spy(monkeypatch, c, create_raises=ValueError("model does not exist"))

    with pytest.raises(ValueError, match="does not exist"):
        c.analyze("p", max_tokens=4096)

    assert seen["stream"] == []


# ── the streamed transport itself, after review 2026-09-10 ───────────────────
#
# _create_stream had `get_final_message()` INSIDE the `except TypeError: pass`
# that exists to detect an SDK too old for the thinking parameter. anthropic
# 0.112.0 raises reachable TypeErrors while accumulating the SSE events (e.g.
# `content.text += event.delta.text` against a null delta), so such an error
# was read as "SDK too old" and answered by silently re-issuing the ENTIRE
# generation — with thinking no longer disabled, and the first attempt's
# streamed output tokens billed by the provider but counted nowhere. Harmless
# while this was the truncation-retry path; not harmless once the streaming
# fix made it the primary transport for every call over the ceiling.


class _Mgr:
    def __init__(self, raiser):
        self._raiser = raiser

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        if self._raiser:
            raise self._raiser
        return _msg()


def test_an_accumulation_typeerror_is_not_read_as_an_old_sdk(monkeypatch):
    c = _client(monkeypatch)
    calls = []

    def stream(**kwargs):
        calls.append(kwargs.get("thinking"))
        # The SDK's own accumulator shape: a TypeError raised while consuming.
        return _Mgr(TypeError("can only concatenate str (not \"NoneType\") to str"))

    monkeypatch.setattr(c.client.messages, "stream", stream)

    with pytest.raises(TypeError, match="concatenate"):
        c._create_stream("sys", [{"role": "user", "content": "p"}], 30_000)

    assert len(calls) == 1, (
        f"the generation must not be re-issued; stream() was called {len(calls)}x"
    )


def test_a_genuinely_old_sdk_still_falls_back(monkeypatch):
    """The behaviour the probe exists for must survive: a TypeError raised when
    BINDING the kwargs (not while streaming) still retries without `thinking`."""
    c = _client(monkeypatch)
    calls = []

    def stream(**kwargs):
        calls.append(kwargs.get("thinking"))
        if "thinking" in kwargs:
            raise TypeError("unexpected keyword argument 'thinking'")
        return _Mgr(None)

    monkeypatch.setattr(c.client.messages, "stream", stream)

    out = c._create_stream("sys", [{"role": "user", "content": "p"}], 30_000)

    assert out.stop_reason == "end_turn"
    assert calls == [{"type": "disabled"}, None], "probed, then retried bare"


def test_the_streamed_retry_is_logged(monkeypatch, caplog):
    """It used to retry in silence while the non-streaming loop logged. Three
    overloads and ~16 s of backoff on a real lesson left nothing in the logs —
    the same species of silence that hid the outage this file is about."""
    c = _client(monkeypatch)
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    attempts = {"n": 0}

    def create_stream(system, messages, max_tokens):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise cc.RateLimitError.__new__(cc.RateLimitError)
        return _msg()

    monkeypatch.setattr(c, "_create_stream", create_stream)

    with caplog.at_level("WARNING"):
        out = c._stream_messages(system="s", messages=[], max_tokens=30_000, retries=3)

    assert out.stop_reason == "end_turn"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("streamed path" in m and "30000" in m for m in msgs), msgs


def test_every_public_method_is_covered(monkeypatch, tmp_path):
    """The reason the fix sits in _call_messages. A vision call asked to return
    a long transcription would have hit exactly the same wall."""
    c = _client(monkeypatch)
    seen = _spy(monkeypatch, c)
    png = tmp_path / "page.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    c.transcribe_images([png], "transcribe", max_tokens=30_000)
    c.analyze_images_batch([png], "read", max_tokens=30_000)
    c.analyze_image(png, "read", max_tokens=30_000)

    assert seen["stream"] == [30_000, 30_000, 30_000]
    assert seen["create"] == []
