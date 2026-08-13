"""Script-family routing: which provider serves which language.

The rule is keyed on SCRIPT, not language, and the case that proves why is
Jawi — Malay written in the Arabic script. Its Malay-ness says nothing about
which model renders it correctly; its Arabic script says everything. Haiku got
Jawi wrong while handling Arabic fine, which is why worker/process.py has
carried a hard-coded Jawi exception since 2026-07-19.
"""
from __future__ import annotations

import pytest

from shared.model_routing import (
    ANTHROPIC,
    GEMINI,
    KIMI,
    provider_for,
    provider_is_available,
    script_family,
)
from shared.languages import LANGUAGES


# ── the three families ───────────────────────────────────────────────────

@pytest.mark.parametrize("code", ["ar", "ur", "fa", "ps", "ckb", "sd", "ms-arab"])
def test_arabic_script_goes_to_claude(code):
    assert provider_for(code) == ANTHROPIC


@pytest.mark.parametrize("code", ["en", "ms", "fr", "es", "pt", "hi", "mr", "te"])
def test_everything_else_goes_to_gemini(code):
    assert provider_for(code) == GEMINI


@pytest.mark.parametrize("code", ["zh", "zh-hans", "zh-hant", "ja"])
def test_han_scripts_are_reserved_for_kimi(code):
    assert provider_for(code) == KIMI


# ── the case the whole design exists for ─────────────────────────────────

def test_jawi_routes_on_its_SCRIPT_not_its_language():
    """Jawi is Malay. Malay goes to Gemini. Jawi must NOT."""
    assert provider_for("ms") == GEMINI
    assert provider_for("ms-arab") == ANTHROPIC


def test_arabic_and_jawi_share_a_provider_despite_unrelated_languages():
    assert provider_for("ar") == provider_for("ms-arab") == ANTHROPIC


# ── every language the app actually serves is routed ─────────────────────

def test_every_supported_language_has_an_implemented_provider():
    """No shipped language may route to a provider we have not built."""
    for code in LANGUAGES:
        provider = provider_for(code)
        assert provider_is_available(provider), f"{code} routes to unimplemented {provider}"


def test_the_live_split_is_what_we_intend():
    by_provider: dict[str, set[str]] = {}
    for code in LANGUAGES:
        by_provider.setdefault(provider_for(code), set()).add(code)
    assert by_provider[ANTHROPIC] == {"ar", "ms-arab"}
    assert by_provider[GEMINI] == {"en", "ms", "fr", "es", "pt", "hi", "mr", "te"}
    assert KIMI not in by_provider, "no shipped language may route to Kimi yet"


# ── robustness ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("code", ["AR", "  ar  ", "ms_arab", "MS-ARAB"])
def test_case_whitespace_and_underscores_normalise(code):
    assert provider_for(code) == ANTHROPIC


@pytest.mark.parametrize(
    "code,expected",
    [("ar-EG", ANTHROPIC), ("zh-Hans-CN", KIMI), ("en-GB", GEMINI), ("pt-BR", GEMINI)],
)
def test_regional_variants_follow_their_primary_subtag(code, expected):
    assert provider_for(code) == expected


@pytest.mark.parametrize("code", [None, "", "   ", "klingon"])
def test_unknown_and_missing_fall_to_the_default_path(code):
    """A new or unrecognised language gets the well-tested majority provider,
    never an error and never an unevaluated one."""
    assert script_family(code) == "default"
    assert provider_for(code) == GEMINI


def test_korean_is_not_treated_as_han():
    """Hangul is alphabetic — it behaves like the default path, not like Chinese."""
    assert provider_for("ko") == GEMINI
