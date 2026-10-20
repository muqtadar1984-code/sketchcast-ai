"""Nothing defaults to Sonnet 4.6 (founder, 2026-10-20).

Railway already sets CLAUDE_MODEL, ARTIFACT_MODEL and
CATALOGUE_CLAUDE_MODEL_TEXT to claude-sonnet-5-5. These pins are for the day
a variable is deleted: the code default must not fall back to the retired
model. Sonnet 4.6 stays mapped on Bedrock and priced, for rollback only.
"""
from __future__ import annotations

import anthropic
import pytest

from shared import claude_client as cc
from shared import llm
from shared.claude_client import ClaudeClient, MODEL_PRICING


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("CLAUDE_MODEL", "LLM_BACKEND", "BEDROCK_MODEL_MAP"):
        monkeypatch.delenv(var, raising=False)
    for role in ("ANALYSIS", "SCRIPT", "TEXT", "WORKSHEET"):
        monkeypatch.delenv("CATALOGUE_CLAUDE_MODEL_" + role, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


def test_the_general_client_defaults_to_sonnet_5_5():
    assert ClaudeClient().model == "claude-sonnet-5-5"


@pytest.mark.parametrize("role", ["analysis", "script", "text", "worksheet"])
def test_the_catalogue_sonnet_roles_default_to_sonnet_5_5(role):
    assert llm.catalogue_claude_model(role) == "claude-sonnet-5-5"


def test_sonnet_5_5_is_priced_at_its_own_rate():
    """Unpriced it was booked at the $3/$15 default, 1.5x its real cost."""
    assert MODEL_PRICING["claude-sonnet-5-5"] == (2.0, 10.0)


def test_the_default_runs_on_bedrock_with_thinking_off_spelled_for_5_5(monkeypatch):
    monkeypatch.setattr(anthropic, "AnthropicBedrock", lambda **kw: object(), raising=False)
    monkeypatch.setenv("LLM_BACKEND", "bedrock")
    c = ClaudeClient()
    assert c.backend == "bedrock"
    assert c._api_model == "global.anthropic.claude-sonnet-5-5"
    assert c._thinking_off == {"type": "between_tools"}


def test_sonnet_4_6_stays_reachable_for_rollback():
    assert "claude-sonnet-4-6" in cc._BEDROCK_MODEL_IDS
    assert "claude-sonnet-4-6" in MODEL_PRICING
