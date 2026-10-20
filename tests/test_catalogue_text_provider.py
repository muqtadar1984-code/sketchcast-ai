"""The catalogue text seam: CATALOGUE_TEXT_PROVIDER moves ALL catalogue text
work (kit pieces, topic jobs, the Kit Translator) to Claude — on Bedrock
when LLM_BACKEND=bedrock — and nothing else.

Three things these tests exist to hold:
- the merge is INERT: with the variable unset, every client is what it was;
- a teacher's BOOK job never moves, whatever the variable says;
- a catalogue job on Claude gets a Claude model for every role — never a
  Gemini id handed to the Claude client (a 400 on every call) — and every
  default is one Bedrock serves (an unmapped model silently bills Anthropic).
"""
from __future__ import annotations

import pytest

import anthropic

from shared import claude_client as cc
from shared import llm, text_models
from shared.claude_client import ClaudeClient
from shared.gemini_client import GeminiClient
from tests.test_observer_job_guard import _builder, _fresh
from worker import client as db
import worker.run as run


class _StubBedrock:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(anthropic, "AnthropicBedrock", _StubBedrock, raising=False)
    for var in ("CATALOGUE_TEXT_PROVIDER", "LLM_BACKEND", "BEDROCK_MODEL_MAP", "CLAUDE_MODEL",
                "ARTIFACT_MODEL", "SCRIPT_FALLBACK_MODEL"):
        monkeypatch.delenv(var, raising=False)
    for role in ("ANALYSIS", "SCRIPT", "TEXT", "DECK", "WORKSHEET", "ACTIVITY"):
        monkeypatch.delenv("CATALOGUE_CLAUDE_MODEL_" + role, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "sketchcast")
    llm.set_catalogue_scope(False)
    llm.set_script_fallback(False)
    yield
    llm.set_catalogue_scope(False)
    llm.set_script_fallback(False)


def _claude_on(monkeypatch):
    monkeypatch.setenv("CATALOGUE_TEXT_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_BACKEND", "bedrock")


# ── inert by default ─────────────────────────────────────────────────────
class TestInertUntilCutOver:
    def test_catalogue_english_stays_on_gemini_with_the_variable_unset(self):
        llm.set_catalogue_scope(True)
        assert isinstance(llm.client_for("en"), GeminiClient)
        assert llm.analysis_client("en").model == text_models.resolve(text_models.ANALYSIS).id
        assert llm.script_client("en").model == text_models.resolve(text_models.SCRIPT).id

    def test_catalogue_arabic_keeps_its_script_routing(self):
        llm.set_catalogue_scope(True)
        assert isinstance(llm.client_for("ar"), ClaudeClient)

    def test_an_unknown_value_is_gemini(self, monkeypatch):
        monkeypatch.setenv("CATALOGUE_TEXT_PROVIDER", "bedrock")
        llm.set_catalogue_scope(True)
        assert llm.catalogue_text_provider() == llm.GEMINI
        assert isinstance(llm.client_for("en"), GeminiClient)


# ── book jobs never move ─────────────────────────────────────────────────
class TestBookJobsNeverMove:
    def test_english_book_job_stays_on_gemini_with_the_variable_on(self, monkeypatch):
        _claude_on(monkeypatch)
        assert llm.catalogue_on_claude() is False
        assert isinstance(llm.client_for("en"), GeminiClient)
        assert isinstance(llm.client_for("hi", kind="deck"), GeminiClient)
        assert llm.analysis_client("en").model == text_models.resolve(text_models.ANALYSIS).id
        assert llm.script_client("en").model == text_models.resolve(text_models.SCRIPT).id

    def test_arabic_book_job_keeps_the_book_models(self, monkeypatch):
        _claude_on(monkeypatch)
        assert llm.client_for("ar", kind="deck").model == cc.artifact_model("deck")
        assert llm.client_for("ar").model == "claude-sonnet-5-5"

    def test_the_script_fallback_still_serves_a_teachers_job(self, monkeypatch):
        _claude_on(monkeypatch)
        llm.set_script_fallback(True)
        assert llm.script_client("en").model == text_models.FLASH_LITE_3_5


# ── catalogue jobs on Claude ─────────────────────────────────────────────
class TestCatalogueOnClaude:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        _claude_on(monkeypatch)
        llm.set_catalogue_scope(True)

    @pytest.mark.parametrize("lang", ["en", "hi", "ms", "ar", "ms-arab"])
    def test_every_language_goes_to_claude_on_bedrock(self, lang):
        c = llm.client_for(lang)
        assert isinstance(c, ClaudeClient)
        assert c.backend == "bedrock"
        assert c.model == llm.CATALOGUE_SONNET

    def test_per_role_defaults(self):
        assert llm.analysis_client("en").model == llm.CATALOGUE_SONNET
        assert llm.script_client("en").model == llm.CATALOGUE_SONNET
        assert llm.client_for("en", kind="worksheet").model == llm.CATALOGUE_SONNET
        for kind in ("deck", "activity", "case_study", "lesson_plan"):
            assert llm.client_for("en", kind=kind).model == llm.CATALOGUE_HAIKU, kind
        # Jawi's exception (kind=None) asks for the stronger model, and gets it
        assert llm.client_for("ms-arab", kind=None).model == llm.CATALOGUE_SONNET

    def test_every_default_is_served_by_bedrock(self):
        for role in ("analysis", "script", "text", "worksheet", "deck"):
            model = llm.catalogue_claude_model(role)
            assert model in cc._BEDROCK_MODEL_IDS, model
            c = llm.client_for("en", kind=role) if role not in ("analysis", "script") else (
                llm.analysis_client("en") if role == "analysis" else llm.script_client("en"))
            assert c.backend == "bedrock" and c._api_model.startswith("global.anthropic."), role

    def test_role_models_are_env_overridable(self, monkeypatch):
        monkeypatch.setenv("CATALOGUE_CLAUDE_MODEL_SCRIPT", "claude-sonnet-5")
        monkeypatch.setenv("CATALOGUE_CLAUDE_MODEL_DECK", "claude-sonnet-4-6")
        assert llm.script_client("en").model == "claude-sonnet-5"
        assert llm.client_for("en", kind="deck").model == "claude-sonnet-4-6"

    def test_a_gemini_id_in_the_variable_is_refused(self, monkeypatch):
        monkeypatch.setenv("CATALOGUE_CLAUDE_MODEL_ANALYSIS", "gemini-3.5-flash")
        assert llm.analysis_client("en").model == llm.CATALOGUE_SONNET

    def test_a_pinned_gemini_model_is_not_handed_to_claude(self):
        c = llm.client_for("en", model=text_models.resolve(text_models.SCRIPT).id)
        assert isinstance(c, ClaudeClient) and c.model == llm.CATALOGUE_SONNET

    def test_a_pinned_claude_model_is_honoured(self):
        assert llm.client_for("en", model="claude-haiku-4-5").model == "claude-haiku-4-5"

    def test_the_script_fallback_never_takes_catalogue_work_back_to_gemini(self):
        llm.set_script_fallback(True)
        assert isinstance(llm.script_client("en"), ClaudeClient)

    def test_cjk_is_served_rather_than_refused(self):
        # Kimi has no client; catalogue work names its provider explicitly.
        assert isinstance(llm.client_for("ja"), ClaudeClient)


# ── the dispatch sets the scope per job ──────────────────────────────────
class TestDispatchSetsTheScope:
    @pytest.mark.parametrize("job_type", ["topic_article", "topic_derive", "topic_questions",
                                          "topic_publish", "kit_translate"])
    def test_catalogue_job_types_are_in_scope(self, job_type):
        assert run._catalogue_text_scope({"type": job_type}) is True

    def test_a_kit_generation_is_in_scope_by_its_flag(self):
        assert run._catalogue_text_scope({"type": "presentation", "params": {"catalogue": True}}) is True
        assert run._catalogue_text_scope({"type": "deck", "params": {"catalogue": "true"}}) is True

    @pytest.mark.parametrize("job_type", ["presentation", "worksheet", "deck", "index_book", "support_diagnose"])
    def test_a_teachers_job_is_not(self, job_type):
        assert run._catalogue_text_scope({"type": job_type, "params": {}}) is False

    def _drive(self, monkeypatch, job: dict) -> dict:
        seen: dict = {}

        def capture(sb, j, *a):
            seen["on_claude"] = llm.catalogue_on_claude()
            seen["client"] = llm.client_for("en")

        queue = [job]
        monkeypatch.setattr(db, "claim_next_sketch", lambda sb: None)
        monkeypatch.setattr(db, "claim_next_job", lambda sb, **kw: queue.pop(0) if queue else None)
        monkeypatch.setattr(db, "claim_catalogue_observer_job", lambda *a, **kw: None)
        monkeypatch.setattr(run, "_claim_catalogue_generation", lambda sb: None)
        monkeypatch.setattr(run, "_catalogue_observer_types", lambda sb: [])
        monkeypatch.setattr(run, "process_generation", capture)
        import catalogue.article
        monkeypatch.setattr(catalogue.article, "run_article_job", capture)
        run._shutdown.clear()
        assert run.run_once(_fresh("queued", jobs=[job])) is True
        return seen

    def test_a_topic_job_runs_on_claude_and_the_scope_is_cleared_after(self, monkeypatch):
        _claude_on(monkeypatch)
        seen = self._drive(monkeypatch, {**_builder("job-a", "topic_article"), "generation_id": None})
        assert seen["on_claude"] is True and isinstance(seen["client"], ClaudeClient)
        assert llm.catalogue_scope_on() is False

    def test_a_kit_generation_runs_on_claude(self, monkeypatch):
        _claude_on(monkeypatch)
        seen = self._drive(monkeypatch, {**_builder("job-k", "presentation"), "params": {"catalogue": True}})
        assert seen["on_claude"] is True and isinstance(seen["client"], ClaudeClient)

    def test_a_book_job_after_a_catalogue_job_is_back_on_gemini(self, monkeypatch):
        _claude_on(monkeypatch)
        llm.set_catalogue_scope(True)              # left over from a previous job on this thread
        seen = self._drive(monkeypatch, _builder("job-b", "presentation"))
        assert seen["on_claude"] is False and isinstance(seen["client"], GeminiClient)
