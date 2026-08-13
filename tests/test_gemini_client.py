"""GeminiClient: token accounting, response shaping, and interface parity.

The accounting is the part worth pinning. Vertex bills Gemini's thinking tokens
at the OUTPUT rate, and reports them in a THIRD field the Claude shape has no
equivalent for. Measured on a real generation: 5,022 answer tokens against 1,917
thinking tokens. Dropping thoughtsTokenCount would understate that call's cost
by 38% — and 88% of SketchCast's per-generation cost is output tokens, so the
error would land squarely on the number the financial model is built from.
"""
from __future__ import annotations

import pytest

from shared.claude_client import ClaudeClient
from shared.gemini_client import GEMINI_PRICING, GeminiClient


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("VERTEX_PROJECT_ID", "sketchcast")
    monkeypatch.setenv("VERTEX_REGION", "global")
    return GeminiClient(model="gemini-2.5-flash")


def usage_response(prompt=0, answer=0, thoughts=0, text="{}", finish="STOP"):
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                        "finishReason": finish}],
        "usageMetadata": {
            "promptTokenCount": prompt,
            "candidatesTokenCount": answer,
            "thoughtsTokenCount": thoughts,
            "totalTokenCount": prompt + answer + thoughts,
        },
    }


# ── token accounting ─────────────────────────────────────────────────────

def test_thinking_tokens_are_billed_as_output(client):
    """THE regression this file exists for."""
    u = client.track_tokens(usage_response(prompt=20, answer=5_022, thoughts=1_917))
    assert u["output_tokens"] == 5_022 + 1_917, "thoughts must be folded into output"

    in_rate, out_rate = GEMINI_PRICING["gemini-2.5-flash"]
    expected = (20 * in_rate + 6_939 * out_rate) / 1_000_000
    assert u["estimated_cost_usd"] == pytest.approx(round(expected, 6))


def test_ignoring_thoughts_would_understate_cost_by_38_percent(client):
    """Pins the magnitude, so a future 'simplification' that drops the field
    fails loudly rather than quietly shaving a third off recorded spend."""
    with_thoughts = client.track_tokens(usage_response(prompt=20, answer=5_022, thoughts=1_917))
    naive = GeminiClient(model="gemini-2.5-flash")
    without = naive.track_tokens(usage_response(prompt=20, answer=5_022, thoughts=0))
    ratio = with_thoughts["estimated_cost_usd"] / without["estimated_cost_usd"]
    assert 1.35 < ratio < 1.40


def test_session_usage_accumulates_like_claude(client):
    client.track_tokens(usage_response(prompt=100, answer=200, thoughts=50))
    client.track_tokens(usage_response(prompt=10, answer=20, thoughts=5))
    assert client.session_usage["calls"] == 2
    assert client.session_usage["input_tokens"] == 110
    assert client.session_usage["output_tokens"] == 275
    assert client.session_usage["cost_usd"] > 0


def test_usage_dict_has_the_same_keys_as_claude(client):
    """process.py folds one client's session_usage into another's; mismatched
    keys would silently drop a bucket from jobs.usage."""
    gemini_keys = set(client.track_tokens(usage_response(prompt=1, answer=1)))
    claude_keys = {
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "total_tokens", "estimated_cost_usd",
    }
    assert gemini_keys == claude_keys


def test_unknown_model_falls_back_to_a_rate_rather_than_crashing(monkeypatch):
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    c = GeminiClient(model="gemini-99-experimental")
    assert c.track_tokens(usage_response(prompt=1_000_000, answer=0))["estimated_cost_usd"] > 0


# ── response shaping ─────────────────────────────────────────────────────

def test_text_extraction_survives_a_safety_block(client):
    """A blocked candidate carries no `content` — never index blindly."""
    assert client._text({"candidates": [{"finishReason": "SAFETY"}]}) == ""
    assert client._text({}) == ""


def test_text_extraction_reads_the_first_text_part(client):
    assert client._text(usage_response(text='{"ok":true}')) == '{"ok":true}'


def test_json_extraction_is_shared_with_claude(client):
    """Same fenced-JSON handling both sides, so a provider swap can't change
    how a malformed reply is parsed."""
    fenced = '```json\n{"a": 1}\n```'
    assert ClaudeClient._extract_json(fenced) == {"a": 1}


# ── config ───────────────────────────────────────────────────────────────

def test_requires_a_project(monkeypatch):
    monkeypatch.delenv("VERTEX_PROJECT_ID", raising=False)
    with pytest.raises(RuntimeError, match="VERTEX_PROJECT_ID"):
        GeminiClient()


def test_endpoint_targets_vertex_not_ai_studio(client):
    """AI Studio bills to a separate account that GCP credits do not cover."""
    url = client._url()
    assert url.startswith("https://aiplatform.googleapis.com/")
    assert "generativelanguage" not in url
    assert "/projects/sketchcast/locations/global/" in url
    assert url.endswith(":generateContent")


def test_thinking_is_disabled_in_the_request_body(client, monkeypatch):
    """+38% output tokens if this regresses."""
    captured = {}

    class _Res:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return usage_response(prompt=1, answer=1)

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return _Res()

    monkeypatch.setattr("shared.gemini_client.requests.post", fake_post)
    monkeypatch.setattr("shared.gemini_client._access_token", lambda: "tok")
    client.analyze("hello")

    assert captured["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0
