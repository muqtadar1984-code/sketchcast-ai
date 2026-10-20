"""Claude Haiku 5.5 on Bedrock: the profile, the price, the thinking spelling.

Haiku 5.5 is the cost trial's model (~1/20 of Sonnet 5.5's per-token rate).
Three things had to be right before a single kit runs on it:

- the Bedrock profile: an UNMAPPED model silently stays on the Anthropic API
  (ClaudeClient.__init__), so a Railway switch to Haiku 5.5 would have billed
  the Anthropic balance, not the AWS credit;
- the price: missing from MODEL_PRICING it is booked at the Sonnet default,
  30x its real cost, and the trial measures cost from jobs.usage; above a
  100K-token prompt it has a second rate card, 5x the first;
- thinking off: Haiku 5.5 takes {"type": "disabled"} and REFUSES
  {"type": "between_tools"} (live on Bedrock 2026-10-20). Spelled the 5.5
  way, the non-streamed call was rescued by the no-thinking-parameter
  fallback, which on Haiku 5.5 means adaptive thinking ON (billed as
  output), and the streamed call raised. Pinned below: the first request is
  the right one, on both transports, and no fallback runs.
"""
from __future__ import annotations

import types

import anthropic
import pytest

from shared.claude_client import ClaudeClient, MODEL_PRICING, _thinking_off_for

# The refusal as Bedrock returned it for between_tools on Haiku 5.5.
REFUSES_BETWEEN_TOOLS = ('An error occurred (ValidationException) when calling the InvokeModel '
                         'operation: "thinking.type.between_tools" is not supported for this model.')


def _usage(inp=5, out=3, cache_read=0, cache_write=0):
    return types.SimpleNamespace(input_tokens=inp, output_tokens=out,
                                 cache_read_input_tokens=cache_read,
                                 cache_creation_input_tokens=cache_write)


def _msg():
    return types.SimpleNamespace(content=[types.SimpleNamespace(text='{"ok": true}')],
                                 stop_reason="end_turn", usage=_usage())


class _Haiku55:
    """Answers as Haiku 5.5 does: between_tools is a 400, the rest succeeds."""

    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(("create", kw.get("thinking", "ABSENT")))
        if kw.get("thinking") == {"type": "between_tools"}:
            raise RuntimeError(REFUSES_BETWEEN_TOOLS)
        return _msg()

    def stream(self, **kw):
        self.calls.append(("stream", kw.get("thinking", "ABSENT")))
        refused = kw.get("thinking") == {"type": "between_tools"}

        class _Mgr:
            def __enter__(self_inner):
                if refused:
                    raise RuntimeError(REFUSES_BETWEEN_TOOLS)
                return types.SimpleNamespace(get_final_message=_msg)

            def __exit__(self_inner, *a):
                return False

        return _Mgr()


def _client(model="claude-haiku-5-5"):
    c = ClaudeClient.__new__(ClaudeClient)
    c.model, c._api_model, c.backend = model, model, "anthropic"
    c._thinking_off = _thinking_off_for(model)
    c.client = types.SimpleNamespace(messages=_Haiku55())
    c.session_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                       "cost_usd": 0.0}
    c._log_usage = lambda usage: None
    return c


# ── thinking off ─────────────────────────────────────────────────────────

def test_thinking_off_is_disabled_on_haiku_5_5():
    assert _thinking_off_for("claude-haiku-5-5") == {"type": "disabled"}


def test_sonnet_5_5_keeps_between_tools():
    assert _thinking_off_for("claude-sonnet-5-5") == {"type": "between_tools"}


def test_first_request_is_the_right_one_on_both_transports():
    """One call each, thinking disabled: no 400, no fallback request."""
    c = _client()
    c._create("s", [{"role": "user", "content": "x"}], 50)
    c._create_stream("s", [{"role": "user", "content": "x"}], 30000)
    assert c.client.messages.calls == [("create", {"type": "disabled"}),
                                       ("stream", {"type": "disabled"})]


def test_the_old_spelling_was_saved_only_by_the_fallback():
    """What the fix removes: between_tools on Haiku 5.5 is a 400, after which
    the non-streamed call re-sent WITHOUT a thinking parameter (thinking on
    by default on this model) and the streamed call failed."""
    c = _client()
    c._thinking_off = {"type": "between_tools"}
    c._create("s", [{"role": "user", "content": "x"}], 50)
    assert c.client.messages.calls == [("create", {"type": "between_tools"}), ("create", "ABSENT")]
    with pytest.raises(RuntimeError, match="between_tools"):
        c._create_stream("s", [{"role": "user", "content": "x"}], 30000)


# ── Bedrock profile ──────────────────────────────────────────────────────

class _StubBedrock:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_maps_to_the_global_profile(monkeypatch):
    monkeypatch.setattr(anthropic, "AnthropicBedrock", _StubBedrock, raising=False)
    monkeypatch.delenv("BEDROCK_MODEL_MAP", raising=False)
    monkeypatch.setenv("LLM_BACKEND", "bedrock")
    c = ClaudeClient(model="claude-haiku-5-5")
    assert c.backend == "bedrock", "an unmapped model silently stays on the Anthropic API"
    assert c._api_model == "global.anthropic.claude-haiku-5-5"
    assert c.model == "claude-haiku-5-5", "canonical id must survive for pricing"
    assert c._thinking_off == {"type": "disabled"}


# ── price ────────────────────────────────────────────────────────────────

def _cost(**usage):
    return _client().track_tokens(types.SimpleNamespace(usage=_usage(**usage)))["estimated_cost_usd"]


def test_short_prompt_rate():
    assert MODEL_PRICING["claude-haiku-5-5"] == (0.10, 0.50)
    # 50K in + 20K out = 0.005 + 0.010
    assert _cost(inp=50_000, out=20_000) == pytest.approx(0.015)


def test_exactly_100k_is_still_the_short_card():
    assert _cost(inp=100_000, out=0) == pytest.approx(0.010)


def test_long_prompt_rate_applies_to_the_whole_call():
    # 120K in + 10K out at $0.50/$2.50 = 0.060 + 0.025
    assert _cost(inp=120_000, out=10_000) == pytest.approx(0.085)


def test_cached_tokens_count_toward_the_threshold():
    """A 90K cached grounding + 20K fresh is a 110K prompt: the long card,
    with the cache multipliers on ITS input rate."""
    # 20K*0.50 + 90K*0.50*0.1 + 1K*2.50 = 0.010 + 0.0045 + 0.0025
    assert _cost(inp=20_000, cache_read=90_000, out=1_000) == pytest.approx(0.017)
    # cache write at 2x (1-hour TTL): 60K*0.50*2 + 50K*0.50 = 0.060 + 0.025
    assert _cost(inp=50_000, cache_write=60_000, out=0) == pytest.approx(0.085)


def test_other_models_have_no_long_card():
    c = _client("claude-sonnet-4-6")
    cost = c.track_tokens(types.SimpleNamespace(usage=_usage(inp=200_000, out=0)))["estimated_cost_usd"]
    assert cost == pytest.approx(0.60)
