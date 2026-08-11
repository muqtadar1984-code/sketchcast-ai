"""The Vertex AI backend: routing, model-id mapping, and the pricing trap.

LLM_BACKEND=vertex sends the same requests to Claude through Google Cloud so
that GCP credits can pay for generation, and so a dead Anthropic balance stops
being a total outage (2026-08-10: every generation failed for ~13 hours).

The defect these tests exist to prevent is not "the wrong endpoint" — that
fails loudly. It is the SILENT one: Vertex needs an "@version" suffix on dated
snapshots, and if that suffixed id also became the MODEL_PRICING key, every
Haiku call would miss the table, fall back to _DEFAULT_PRICING (Sonnet's
$3/$15), and record ~3x its true cost into jobs.usage.cost_usd — the exact
column the financial model is built on. Nothing would error.
"""
from __future__ import annotations

import json
import os
import sys
import types
from unittest import mock

import pytest

# The Vertex client lives behind an optional extra; stub the symbol so these
# tests exercise OUR routing without requiring google-auth to be installed.
if not hasattr(sys.modules.get("anthropic"), "AnthropicVertex"):
    import anthropic

    class _StubVertex:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    anthropic.AnthropicVertex = _StubVertex  # type: ignore[attr-defined]

from shared import claude_client as cc  # noqa: E402
from shared.claude_client import ClaudeClient, MODEL_PRICING, _DEFAULT_PRICING  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "LLM_BACKEND", "VERTEX_PROJECT_ID", "VERTEX_REGION", "VERTEX_MODEL_MAP",
        "CLAUDE_MODEL", "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_APPLICATION_CREDENTIALS_JSON",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(cc, "_CREDENTIALS_PATH", None, raising=False)


# ── backend routing ──────────────────────────────────────────────────────

def test_defaults_to_anthropic():
    """No env var set must keep production exactly where it is."""
    assert cc.llm_backend() == "anthropic"
    assert ClaudeClient().backend == "anthropic"


def test_vertex_selected_by_env(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "sketchcast-prod")
    assert ClaudeClient().backend == "vertex"


def test_backend_value_is_normalised(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "  VERTEX  ")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    assert ClaudeClient().backend == "vertex"


def test_vertex_without_project_fails_loudly(monkeypatch):
    """Better a clear error at construction than an auth failure per call."""
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    with pytest.raises(RuntimeError, match="VERTEX_PROJECT_ID"):
        ClaudeClient()


def test_vertex_needs_no_anthropic_key(monkeypatch):
    """Vertex authenticates with GCP credentials — an Anthropic key is not
    required, and demanding one would make the failover useless in exactly the
    situation it exists for."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "sketchcast-prod")
    assert ClaudeClient().backend == "vertex"


def test_region_defaults_to_global(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "sketchcast-prod")
    client = ClaudeClient().client
    assert client.kwargs["region"] == "global"
    assert client.kwargs["project_id"] == "sketchcast-prod"


def test_region_override(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    monkeypatch.setenv("VERTEX_REGION", "asia-southeast1")
    assert ClaudeClient().client.kwargs["region"] == "asia-southeast1"


# ── model id mapping ─────────────────────────────────────────────────────

def test_anthropic_backend_leaves_ids_untouched(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-4-5")
    c = ClaudeClient()
    assert c.model == "claude-haiku-4-5"
    assert c._api_model == "claude-haiku-4-5"


def test_vertex_maps_dated_snapshot(monkeypatch):
    """Haiku 4.5 is a dated snapshot: Vertex wants '@20251001', not '-20251001'."""
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-4-5")
    c = ClaudeClient()
    assert c._api_model == "claude-haiku-4-5@20251001"
    assert c.model == "claude-haiku-4-5", "canonical id must survive for pricing"


def test_vertex_passes_current_gen_ids_through(monkeypatch):
    """Current-generation models use the bare first-party id on Vertex."""
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    c = ClaudeClient()
    assert c._api_model == "claude-sonnet-4-6"


def test_model_map_override(monkeypatch):
    """A wrong id must be fixable from the environment, not a redeploy."""
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("VERTEX_MODEL_MAP", json.dumps({"claude-haiku-4-5": "claude-haiku-4-5@20260101"}))
    assert ClaudeClient()._api_model == "claude-haiku-4-5@20260101"


def test_malformed_model_map_raises(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    monkeypatch.setenv("VERTEX_MODEL_MAP", "{not json")
    with pytest.raises(RuntimeError, match="VERTEX_MODEL_MAP"):
        ClaudeClient()


# ── the pricing trap ─────────────────────────────────────────────────────

def _usage(**kw):
    return types.SimpleNamespace(
        input_tokens=kw.get("input_tokens", 1_000_000),
        output_tokens=kw.get("output_tokens", 0),
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def test_vertex_haiku_still_prices_as_haiku(monkeypatch):
    """THE regression this module exists for.

    If the '@'-suffixed id reached MODEL_PRICING it would miss the table and
    silently bill at Sonnet's rate — 3x — into jobs.usage.cost_usd.
    """
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-4-5")
    c = ClaudeClient()

    usage = c.track_tokens(types.SimpleNamespace(usage=_usage()))

    haiku_in = MODEL_PRICING["claude-haiku-4-5"][0]
    assert usage["estimated_cost_usd"] == pytest.approx(haiku_in, rel=1e-9)
    assert c._api_model not in MODEL_PRICING, "the wire id is deliberately not a pricing key"
    assert usage["estimated_cost_usd"] != pytest.approx(_DEFAULT_PRICING[0], rel=1e-9)


def test_cost_identical_across_backends(monkeypatch):
    """Same model, same tokens, same recorded cost — so the Vertex rows stay
    comparable with the 109-generation Sonnet baseline in the financial model."""
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-4-5")
    anthropic_cost = ClaudeClient().track_tokens(
        types.SimpleNamespace(usage=_usage(output_tokens=50_000))
    )["estimated_cost_usd"]

    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    vertex_cost = ClaudeClient().track_tokens(
        types.SimpleNamespace(usage=_usage(output_tokens=50_000))
    )["estimated_cost_usd"]

    assert anthropic_cost == vertex_cost


# ── credentials ──────────────────────────────────────────────────────────

def test_service_account_json_written_to_file(monkeypatch):
    """Railway holds strings; google-auth wants a file path."""
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", '{"type":"service_account"}')
    ClaudeClient()
    path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh) == {"type": "service_account"}


def test_existing_credentials_path_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "sa.json"))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", '{"type":"service_account"}')
    ClaudeClient()
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(tmp_path / "sa.json")


def test_no_credentials_falls_through_to_adc(monkeypatch):
    """Locally, `gcloud auth application-default login` should just work."""
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    ClaudeClient()
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ


# ── the wire call actually uses the mapped id ────────────────────────────

def test_create_sends_the_mapped_id(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "p")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-haiku-4-5")
    c = ClaudeClient()

    seen = {}

    def _create(**kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text="{}")],
            usage=_usage(input_tokens=0),
            stop_reason="end_turn",
        )

    c.client = types.SimpleNamespace(messages=types.SimpleNamespace(create=_create))
    c.analyze("hello")
    assert seen["model"] == "claude-haiku-4-5@20251001"
