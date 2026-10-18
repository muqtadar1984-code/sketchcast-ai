"""The Amazon Bedrock backend: routing, profile-id mapping, per-model fallback
and the pricing trap.

LLM_BACKEND=bedrock sends the same Messages requests to Claude through AWS so
the Activate credits pay for generation instead of the Anthropic balance.

Three defects these tests exist to prevent, all SILENT:
- the Bedrock profile id becoming the MODEL_PRICING key — every call would miss
  the table, fall back to Sonnet's $3/$15 and inflate jobs.usage.cost_usd;
- an unmapped model being sent to Bedrock under its first-party id — a 400 on
  every job for that model;
- the env override being unable to REMOVE a model, so a model Bedrock stops
  serving could only be rescued by a redeploy.
"""
from __future__ import annotations

import json
import logging

import pytest

import anthropic  # noqa: E402

from shared import claude_client as cc  # noqa: E402
from shared.claude_client import ClaudeClient, MODEL_PRICING  # noqa: E402


class _StubBedrock:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _stub_bedrock_client(monkeypatch):
    """Substitute unconditionally (see test_vertex_backend for why)."""
    monkeypatch.setattr(anthropic, "AnthropicBedrock", _StubBedrock, raising=False)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "LLM_BACKEND", "BEDROCK_REGION", "AWS_REGION", "BEDROCK_MODEL_MAP", "CLAUDE_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


def _bedrock(monkeypatch, model: str | None = None):
    monkeypatch.setenv("LLM_BACKEND", "bedrock")
    if model:
        monkeypatch.setenv("CLAUDE_MODEL", model)
    return ClaudeClient()


# ── backend routing ──────────────────────────────────────────────────────

def test_default_stays_on_anthropic():
    """Shipping this must not move a single call until the variable is set."""
    assert ClaudeClient().backend == "anthropic"


def test_bedrock_selected_by_env(monkeypatch):
    c = _bedrock(monkeypatch)
    assert c.backend == "bedrock"
    assert isinstance(c.client, _StubBedrock)


def test_bedrock_needs_no_anthropic_key(monkeypatch):
    """The whole point is that the Anthropic balance can be empty."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _bedrock(monkeypatch).backend == "bedrock"


def test_region_defaults_to_us_east_1(monkeypatch):
    assert _bedrock(monkeypatch).client.kwargs["aws_region"] == "us-east-1"


def test_region_override_order(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "ap-southeast-1")
    assert _bedrock(monkeypatch).client.kwargs["aws_region"] == "ap-southeast-1"
    monkeypatch.setenv("BEDROCK_REGION", "us-west-2")
    assert ClaudeClient().client.kwargs["aws_region"] == "us-west-2"


# ── profile id mapping + the pricing trap ────────────────────────────────

@pytest.mark.parametrize("model,profile", [
    ("claude-haiku-4-5", "global.anthropic.claude-haiku-4-5-20251001-v1:0"),
    ("claude-haiku-4-5-20251001", "global.anthropic.claude-haiku-4-5-20251001-v1:0"),
    ("claude-sonnet-4-6", "global.anthropic.claude-sonnet-4-6"),
    ("claude-sonnet-5", "global.anthropic.claude-sonnet-5"),
])
def test_models_map_to_global_profiles(monkeypatch, model, profile):
    c = _bedrock(monkeypatch, model)
    assert c._api_model == profile
    assert c.model == model, "canonical id must survive for pricing"


def test_explicit_model_argument_maps_too(monkeypatch):
    """artifact_model()/kind routing passes the model in, not via CLAUDE_MODEL."""
    monkeypatch.setenv("LLM_BACKEND", "bedrock")
    c = ClaudeClient(model="claude-haiku-4-5")
    assert c._api_model.startswith("global.anthropic.claude-haiku-4-5")


def test_pricing_keys_on_canonical_id(monkeypatch):
    """Haiku must be priced as Haiku, not fall through to the Sonnet default."""
    c = _bedrock(monkeypatch, "claude-haiku-4-5")
    assert c.model in MODEL_PRICING
    assert MODEL_PRICING[c.model] == (1.0, 5.0)


# ── per-model fallback ───────────────────────────────────────────────────

def test_unmapped_model_stays_on_anthropic(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger=cc.logger.name):
        c = _bedrock(monkeypatch, "claude-opus-4-8")
    assert c.backend == "anthropic"
    assert c._api_model == "claude-opus-4-8"
    assert not isinstance(c.client, _StubBedrock)
    assert "no profile for claude-opus-4-8" in caplog.text


def test_env_map_adds_a_model(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_MAP", json.dumps({"claude-opus-4-8": "global.anthropic.claude-opus-4-8"}))
    c = _bedrock(monkeypatch, "claude-opus-4-8")
    assert c.backend == "bedrock"
    assert c._api_model == "global.anthropic.claude-opus-4-8"


def test_env_map_overrides_a_profile(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_MAP", json.dumps({"claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6"}))
    assert _bedrock(monkeypatch, "claude-sonnet-4-6")._api_model == "us.anthropic.claude-sonnet-4-6"


def test_env_map_empty_value_removes_a_model(monkeypatch):
    """A model Bedrock stops serving must be sendable back without a redeploy."""
    monkeypatch.setenv("BEDROCK_MODEL_MAP", json.dumps({"claude-sonnet-5": ""}))
    assert _bedrock(monkeypatch, "claude-sonnet-5").backend == "anthropic"


def test_malformed_map_raises(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_MAP", "{not json")
    with pytest.raises(RuntimeError, match="BEDROCK_MODEL_MAP"):
        _bedrock(monkeypatch, "claude-sonnet-4-6")


def test_vertex_map_does_not_leak_into_bedrock(monkeypatch):
    """The two backends' maps are independent."""
    monkeypatch.setenv("VERTEX_MODEL_MAP", json.dumps({"claude-sonnet-4-6": "claude-sonnet-4-6@x"}))
    assert _bedrock(monkeypatch, "claude-sonnet-4-6")._api_model == "global.anthropic.claude-sonnet-4-6"


# ── the real SDK class (no stub) ─────────────────────────────────────────

def test_real_sdk_class_constructs(monkeypatch):
    """The [bedrock] extra is installed and the class accepts our kwargs —
    guards the requirements line, which a stubbed suite would never notice."""
    monkeypatch.undo()
    import importlib

    real = importlib.import_module("anthropic").AnthropicBedrock
    pytest.importorskip("boto3")
    client = real(aws_region="us-east-1", aws_access_key="AKIATEST", aws_secret_key="secret")
    assert hasattr(client, "messages")
