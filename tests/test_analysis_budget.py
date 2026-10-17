"""The combined analysis call's output budget follows the lesson language's
script (2026-10-17): every document of the Arabic Cells kit hit the 16,000
cap sized for English, parsed as nothing, and paid the doubled streamed
retry (~16k wasted tokens and 4–5 minutes per document). A reply in Arabic,
Devanagari or Telugu is about twice the tokens of the same reply in English,
so the first call now asks for twice the budget; the client's truncation
retry stays underneath as the safety net.

The model is a stub that records ``max_tokens`` and answers one concept.
"""

from __future__ import annotations

from agent2_analysis import analyzer
from catalogue import translate_check
from shared.languages import NON_LATIN_SCRIPTS, OUTPUT_TOKEN_FACTOR_NON_LATIN, output_token_factor, script_of

_REPLY = {"concepts": [{"concept_id": "c001", "name": "Cell", "definition": "The unit of life.",
                        "first_introduced_page": 0, "importance": "foundational", "related_concepts": []}],
          "dependencies": [], "prerequisites": [], "difficulty_assessments": [], "visual_opportunities": []}


class _Stub:
    model = "stub"

    def __init__(self):
        self.budgets: list[int] = []
        self.session_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    def analyze(self, prompt, max_tokens=0, **k):
        self.budgets.append(max_tokens)
        return {"data": dict(_REPLY), "usage": {}}


def _chapter() -> dict:
    return {"chapter_num": 1, "title": "Cells", "images": [],
            "sections": [{"title": "What a cell is", "content": " ".join(["cell"] * 600), "subsections": []}]}


def _run(monkeypatch, language: str) -> list[int]:
    stub = _Stub()
    monkeypatch.setattr(analyzer, "save_analysis", lambda a: None)
    monkeypatch.setattr(analyzer, "analyze_images_batch", lambda chapter, book_id, client: ([], {}))
    analyzer.run_full_analysis(book_id="b", chapter_content=_chapter(), client=stub, language=language)
    return stub.budgets


class TestAnalysisBudget:
    def test_the_scripts_and_the_factor_are_one_shared_table(self):
        assert script_of("ar") == "arab" and script_of("hi") == "deva" and script_of("te") == "telu"
        assert script_of("ms-arab") == "arab", "Jawi is Malay in the Arabic script"
        assert script_of("en") is None and script_of("fr") is None and script_of(None) is None
        assert output_token_factor("ar") == OUTPUT_TOKEN_FACTOR_NON_LATIN == 2.0
        assert output_token_factor("en") == output_token_factor("ms") == 1.0
        assert translate_check.script_of("ar") == "arab" and translate_check.script_of("fr") is None
        assert translate_check._LANGUAGE_SCRIPT is NON_LATIN_SCRIPTS, "the pre-check reads the same table"

    def test_an_english_analysis_keeps_its_budget(self, monkeypatch):
        assert _run(monkeypatch, "en") == [analyzer.ANALYSIS_MAX_TOKENS] == [16000]
        assert analyzer.analysis_max_tokens("fr") == analyzer.analysis_max_tokens("ms") == 16000

    def test_an_arabic_analysis_asks_for_twice_the_budget_on_its_first_call(self, monkeypatch):
        assert _run(monkeypatch, "ar") == [32000]
        assert analyzer.analysis_max_tokens("hi") == analyzer.analysis_max_tokens("te") == 32000
        # the Claude client streams anything above 20,000 and doubles a
        # truncated reply up to 32,000 — the budget lands exactly where the
        # retry used to end, so nothing the retry could save is now lost
        from shared.claude_client import _NONSTREAMING_MAX_TOKENS
        assert _NONSTREAMING_MAX_TOKENS < 32000
